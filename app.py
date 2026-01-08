import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
import yfinance as yf
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Intelligence Center", page_icon="🛡️")

# Estilos CSS Profesionales
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .card {
        background-color: #1f2937; 
        padding: 20px; border-radius: 10px; 
        border: 1px solid #374151; text-align: center; height: 100%;
    }
    .metric-label {color: #aaa; font-size: 0.8rem; font-weight: bold;}
    .metric-value {font-size: 1.6rem; font-weight: bold; margin: 0;}
    .section-header {
        background-color: #238636; color: white; padding: 5px 15px; 
        border-radius: 5px; margin: 20px 0 10px 0; font-weight: bold;
    }
    .pmcc-summary-card {
        background-color: #161b22; border: 1px solid #30363d;
        padding: 10px; border-radius: 8px; text-align: center;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🛡️ Portfolio Intelligence & PMCC Accountant")

# --- INICIALIZAR HISTORIAL EN MEMORIA (Gráficos de sesión) ---
if 'history_df' not in st.session_state:
    st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])

# --- SIDEBAR ---
with st.sidebar:
    st.header("📡 Conexión Broker")
    TRADIER_TOKEN = st.text_input("Tradier Access Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    if st.button("🗑️ Reiniciar Sesión"):
        st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])
        st.rerun()
    st.caption("v11.7.0 | Bulletproof Auditor")

# --- FUNCIONES DE APOYO ---
def get_headers(): return {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

def get_underlying_symbol(symbol):
    if len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def clean_data(df):
    if isinstance(df, pd.Series): df = df.to_frame()
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    col = 'Adj Close' if 'Adj Close' in df.columns else 'Close'
    df = df[[col]].copy()
    df.index = df.index.tz_localize(None)
    return df

@st.cache_data(ttl=3600)
def calculate_beta(ticker, spy_returns):
    if ticker in ['BIL', 'SGOV', 'SHV']: return 0.0
    try:
        sym = '^GSPC' if ticker in ['SPX', 'SPXW'] else ticker
        stock_raw = yf.download(sym, period="1y", progress=False)
        if stock_raw.empty: return 1.0
        ret = clean_data(stock_raw).pct_change().dropna()
        aligned = pd.concat([ret, spy_returns], axis=1, join='inner').dropna()
        return aligned.iloc[:,0].cov(aligned.iloc[:,1]) / aligned.iloc[:,1].var()
    except: return 1.0

# --- MOTOR DE DATOS ---
def run_full_analysis():
    # 1. Cuenta
    r_p = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p.status_code != 200: return None
    acct = r_p.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']
    
    r_b = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
    net_liq = float(r_b.json()['balances']['total_equity'])

    # 2. Posiciones
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw_pos = r_pos.json().get('positions', {}).get('position', [])
    if not raw_pos or raw_pos == 'null': raw_pos = []
    if isinstance(raw_pos, dict): raw_pos = [raw_pos]

    # 3. Historial (Contabilidad)
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", params={'limit': 1000}, headers=get_headers())
    raw_hist = r_hist.json().get('history', {}).get('event', []) if r_hist.status_code == 200 else []
    if isinstance(raw_hist, dict): raw_hist = [raw_hist]

    # 4. Market Data
    all_syms = list(set(["SPY"] + [p['symbol'] for p in raw_pos] + [get_underlying_symbol(p['symbol']) for p in raw_pos]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q.status_code == 200 else {}
    spy_p = float(m_map.get('SPY', {}).get('last', 685))

    # 5. Yahoo para Betas
    spy_df = yf.download("SPY", period="1y", progress=False)
    spy_ret = clean_data(spy_df).pct_change().dropna()

    # 6. Procesamiento
    total_rd, total_th, total_exp, total_bwd = 0, 0, 0, 0
    ticker_risk = {}
    detailed = []

    for p in raw_pos:
        sym = p['symbol']
        qty = float(p['quantity'])
        u_sym = get_underlying_symbol(sym)
        m_d = m_map.get(sym, {})
        u_p = float(m_map.get(u_sym, {}).get('last', 0))
        is_opt = len(sym) > 5
        mult = 100 if is_opt else 1
        
        d = float(m_d.get('greeks', {}).get('delta', 1.0 if not is_opt else 0))
        th = float(m_d.get('greeks', {}).get('theta', 0))
        ex = float(m_d.get('greeks', {}).get('extrinsic', 0))
        
        if u_sym not in ticker_risk:
            ticker_risk[u_sym] = {'d_usd': 0, 'th_usd': 0, 'd_puro': 0, 'beta': calculate_beta(u_sym, spy_ret), 'price': u_p}
        
        ticker_risk[u_sym]['d_usd'] += (qty * d * mult * u_p)
        ticker_risk[u_sym]['th_usd'] += (qty * th * mult)
        ticker_risk[u_sym]['d_puro'] += (qty * d * mult)
        total_rd += (qty * d * mult)
        total_th += (qty * th * mult)
        
        detailed.append({
            "Symbol": sym, "Qty": qty, "Underlying": u_sym, "Type": "option" if is_opt else "stock",
            "Delta": d, "Theta": th, "Price": u_p, "Extrinsic": ex, "Strike": m_d.get('strike', 0),
            "Exp": m_d.get('expiration_date', 'N/A'), "Cost": abs(float(p.get('cost_basis', 0)))
        })

    for s, data in ticker_risk.items():
        total_bwd += (data['d_usd'] * data['beta']) / spy_p if spy_p > 0 else 0
        total_exp += abs(data['d_usd'])

    return {
        "net_liq": net_liq, "rd": total_rd, "bwd": total_bwd, "th": total_th, 
        "lev": total_exp/net_liq if net_liq > 0 else 0, "r_map": ticker_risk, 
        "detailed": detailed, "history": raw_hist, "spy_p": spy_p
    }

# --- INTERFAZ ---
t1, t2 = st.tabs(["📊 Riesgo & Historial", "🏗️ PMCC Accountant"])

if TRADIER_TOKEN:
    if st.button("🚀 ACTUALIZAR SISTEMA"):
        data = run_full_analysis()
        if data:
            # Snapshot para gráficos
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([{
                "Timestamp": datetime.now().strftime("%H:%M:%S"), "Net_Liq": data['net_liq'], 
                "Delta_Neto": data['rd'], "BWD_SPY": data['bwd'], "Theta_Diario": data['th'], "Apalancamiento": data['lev']
            }])], ignore_index=True)

            with t1:
                st.markdown(f"### 🏦 Balance Neto: ${data['net_liq']:,.2f}")
                c1, c2, c3, c4 = st.columns(4)
                c1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value">{data["rd"]:.1f}</div></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value">{data["bwd"]:.1f}</div></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value">${data["th"]:.2f}</div></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="card" style="border-bottom:5px solid {"#4ade80" if data["lev"] < 1.5 else "#facc15"}"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value">{data["lev"]:.2f}x</div></div>', unsafe_allow_html=True)
                
                h = st.session_state.history_df
                if len(h) > 1:
                    st.subheader("📈 Tendencia")
                    g1, g2 = st.columns(2)
                    g1.area_chart(h, x="Timestamp", y="Net_Liq")
                    g2.line_chart(h, x="Timestamp", y="BWD_SPY")

                st.subheader("📊 Riesgo por Activo")
                r_rows = [{"Activo": k, "Beta": v['beta'], "Delta Puro": v['d_puro'], "Net Delta $": v['d_usd'], "BWD": (v['d_usd']*v['beta'])/data['spy_p']} for k,v in data['r_map'].items()]
                st.dataframe(pd.DataFrame(r_rows).style.format({"Beta":"{:.2f}", "Net Delta $":"${:,.0f}", "BWD":"{:.2f}"}), use_container_width=True)

            with t2:
                st.subheader("🏗️ Contabilidad PMCC")
                df_d = pd.DataFrame(data['detailed'])
                if not df_d.empty:
                    df_opt = df_d[df_d['Type'] == "option"]
                    for und, group in df_opt.groupby('Underlying'):
                        longs = group[(group['Qty'] > 0) & (group['Delta'].abs() > 0.60)]
                        shorts = group[(group['Qty'] < 0) & (group['Delta'].abs() < 0.50)]
                        
                        if not longs.empty:
                            realized_pnl = 0
                            closed_trades = []
                            # FILTRO SEGURO PARA HISTORIAL
                            asset_hist = [h for h in data['history'] if h.get('type')=='trade' and 'symbol' in h and get_underlying_symbol(h['symbol'])==und]
                            
                            if asset_hist:
                                df_ah = pd.DataFrame(asset_hist)
                                if 'symbol' in df_ah.columns:
                                    for sym, events in df_ah.groupby('symbol'):
                                        evs = events.sort_values('date')
                                        temp_sto = None
                                        for _, row in evs.iterrows():
                                            if row['side'] == 'sell_to_open': temp_sto = row
                                            elif row['side'] == 'buy_to_close' and temp_sto is not None:
                                                pnl = (abs(float(temp_sto['price'])) - abs(float(row['price']))) * 100 * abs(float(row['quantity']))
                                                realized_pnl += pnl
                                                closed_trades.append({"STO": temp_sto['date'][:10], "BTC": row['date'][:10], "Strike": sym[-8:], "P/L": pnl})
                                                temp_sto = None

                            l_cost = longs['Cost'].sum()
                            l_val = (longs['Price'] * longs['Qty'] * 100).sum()
                            net_inc = (l_val - l_cost) + realized_pnl

                            st.markdown(f'<div class="section-header">ACTIVO: {und}</div>', unsafe_allow_html=True)
                            cont1, cont2, cont3 = st.columns(3)
                            cont1.markdown(f'<div class="pmcc-summary-card"><small>CC REALIZADO</small><br><b style="color:#4ade80">${realized_pnl:,.2f}</b></div>', unsafe_allow_html=True)
                            cont2.markdown(f'<div class="pmcc-summary-card"><small>NET INCOME (P/L)</small><br><b>${net_inc:,.0f}</b></div>', unsafe_allow_html=True)
                            cont3.markdown(f'<div class="pmcc-summary-card"><small>ROI</small><br><b>{(net_inc/l_cost*100):.1f}%</b></div>', unsafe_allow_html=True)
                            
                            st.table(longs[['Exp', 'Strike', 'Qty', 'Delta']])
                            if not shorts.empty:
                                sc = shorts.iloc[0]
                                j = sc['Extrinsic'] * 100 * abs(sc['Qty'])
                                st.write(f"🥤 **Short:** K {sc['Strike']} | Exp: {sc['Exp']} | **Jugo: ${j:.2f}**")
                                if j < 15: st.error("⚠️ TIEMPO DE ROLEAR")
                            if closed_trades:
                                with st.expander("📔 Ver Historial"): st.table(pd.DataFrame(closed_trades))
                            st.divider()
                else:
                    st.info("Agrega posiciones de opciones para ver el análisis PMCC.")
            
            with st.expander("Ver Datos Crudos"): st.dataframe(df_d)
else:
    st.info("👈 Ingresa tu Token para iniciar.")


