import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import re
import time
import plotly.graph_objects as go
from datetime import datetime, timedelta

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC & Risk Command Center", page_icon="🛡️")

# Estilos CSS Profesionales (Mezcla de Dashboard Institucional y Hoja Contable)
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

# --- INICIALIZAR HISTORIAL EN MEMORIA (Para gráficos de sesión) ---
if 'history_df' not in st.session_state:
    st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])

# --- SIDEBAR ---
with st.sidebar:
    st.header("📡 Conexión Broker")
    TRADIER_TOKEN = st.text_input("Tradier Access Token", type="password")
    env_mode = st.radio("Entorno", ["Producción (Real)", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción (Real)" else "https://sandbox.tradier.com/v1"
    st.divider()
    if st.button("🗑️ Reiniciar Historial de Sesión"):
        st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])
        st.rerun()

# --- FUNCIONES DE APOYO ---
def get_headers(): return {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

def map_to_yahoo(symbol):
    s = symbol.upper().strip()
    if s in ['SPX', 'SPXW', 'SPX.X']: return '^GSPC'
    if s in ['NDX', 'NDXW', 'NDX.X']: return '^NDX'
    if s in ['RUT', 'RUTW', 'RUT.X']: return '^RUT'
    return s.replace('/', '-')

def get_underlying_symbol(symbol):
    if len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

@st.cache_data(ttl=3600)
def calculate_beta_fixed(ticker, spy_returns):
    if ticker in ['BIL', 'SGOV', 'SHV']: return 0.0
    try:
        stock_raw = yf.download(map_to_yahoo(ticker), period="1y", progress=False)
        stock_raw.index = stock_raw.index.tz_localize(None)
        col = 'Adj Close' if 'Adj Close' in stock_raw.columns else 'Close'
        ret = stock_raw[col].pct_change().dropna()
        aligned = pd.concat([ret, spy_returns], axis=1, join='inner').dropna()
        return aligned.iloc[:,0].cov(aligned.iloc[:,1]) / aligned.iloc[:,1].var()
    except: return 1.0

# --- EL MOTOR DE DATOS UNIFICADO ---
def run_full_analysis():
    # 1. Datos Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    acct_id = r_profile.json()['profile']['account']['account_number'] if isinstance(r_profile.json()['profile']['account'], dict) else r_profile.json()['profile']['account'][0]['account_number']
    
    r_bal = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
    net_liq = float(r_bal.json()['balances']['total_equity'])

    # 2. Posiciones Actuales
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw_pos = r_pos.json().get('positions', {}).get('position', [])
    if not raw_pos or raw_pos == 'null': raw_pos = []
    if isinstance(raw_pos, dict): raw_pos = [raw_pos]

    # 3. Historial para Contabilidad (Últimos 90 días)
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", params={'limit': 100}, headers=get_headers())
    raw_hist = r_hist.json().get('history', {}).get('event', []) if r_hist else []
    if isinstance(raw_hist, dict): raw_hist = [raw_hist]

    # 4. Market Data (Quotes & Greeks)
    all_syms = ["SPY"] + [p['symbol'] for p in raw_pos] + [get_underlying_symbol(p['symbol']) for p in raw_pos]
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(list(set(all_syms))), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}
    spy_p = float(m_map.get('SPY', {}).get('last', 685))

    # 5. Yahoo Data para Beta
    spy_df = yf.download("SPY", period="1y", progress=False)
    spy_col = 'Adj Close' if 'Adj Close' in spy_df.columns else 'Close'
    spy_ret = spy_df[spy_col].tz_localize(None).pct_change().dropna()

    # --- PROCESO DE RIESGO Y GREEKS ---
    total_raw_delta, total_theta, total_abs_exp, total_bwd = 0, 0, 0, 0
    ticker_risk_map = {}
    detailed_positions = []

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
        ext = float(m_d.get('greeks', {}).get('extrinsic', 0))
        
        if u_sym not in ticker_risk_map:
            ticker_risk_map[u_sym] = {'d_usd': 0, 'th_usd': 0, 'd_puro': 0, 'beta': calculate_beta_fixed(u_sym, spy_ret), 'price': u_p}
        
        ticker_risk_map[u_sym]['d_usd'] += (qty * d * mult * u_p)
        ticker_risk_map[u_sym]['th_usd'] += (qty * th * mult)
        ticker_risk_map[u_sym]['d_puro'] += (qty * d * mult)
        total_raw_delta += (qty * d * mult)
        total_theta += (qty * th * mult)
        
        detailed_positions.append({
            "Símbolo": sym, "Qty": qty, "Underlying": u_sym, "Type": "option" if is_opt else "stock",
            "Delta": d, "Theta": th, "Price": u_p, "Extrinsic": ext, "Strike": m_d.get('strike', 0),
            "Exp": m_d.get('expiration_date', 'N/A'), "Cost": abs(float(p.get('cost_basis', 0)))
        })

    for s, data in ticker_risk_map.items():
        total_bwd += (data['d_usd'] * data['beta']) / spy_p if spy_p > 0 else 0
        total_abs_exp += abs(data['d_usd'])

    return {
        "net_liq": net_liq, "total_delta": total_raw_delta, "total_bwd": total_bwd, 
        "total_theta": total_theta, "leverage": total_abs_exp/net_liq, 
        "risk_map": ticker_risk_map, "detailed": detailed_positions, 
        "history": raw_hist, "spy_price": spy_p
    }

# --- UI TABS ---
tab1, tab2 = st.tabs(["📊 Riesgo & Gráficos", "🏗️ PMCC Accountant (Tom King)"])

if TRADIER_TOKEN:
    if st.button("🚀 ACTUALIZAR TODO EL SISTEMA"):
        data = run_full_analysis()
        if data:
            # 1. Grabar en Historial de Sesión
            new_h = {"Timestamp": datetime.now().strftime("%H:%M:%S"), "Net_Liq": data['net_liq'], "Delta_Neto": data['total_delta'], "BWD_SPY": data['total_bwd'], "Theta_Diario": data['total_theta'], "Apalancamiento": data['leverage']}
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([new_h])], ignore_index=True)

            with tab1:
                st.markdown(f"### 🏦 Balance Neto: ${data['net_liq']:,.2f}")
                c1, c2, c3, c4 = st.columns(4)
                colors = ["#4ade80" if x >= 0 else "#f87171" for x in [data['total_delta'], data['total_bwd'], data['total_theta']]]
                c1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value" style="color:{colors[0]}">{data["total_delta"]:.1f}</div></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value" style="color:{colors[1]}">{data["total_bwd"]:.1f}</div></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:{colors[2]}">${data["total_theta"]:.2f}</div></div>', unsafe_allow_html=True)
                lev_c = "#4ade80" if data['leverage'] < 1.5 else "#facc15"
                c4.markdown(f'<div class="card" style="border-bottom:5px solid {lev_c}"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value">{data["leverage"]:.2f}x</div></div>', unsafe_allow_html=True)

                st.divider()
                h_df = st.session_state.history_df
                if len(h_df) > 1:
                    g1, g2 = st.columns(2)
                    with g1: st.write("**Evolución Capital**"); st.area_chart(h_df, x="Timestamp", y="Net_Liq")
                    with g2: st.write("**Riesgo BWD**"); st.line_chart(h_df, x="Timestamp", y="BWD_SPY")

                st.subheader("📊 Riesgo Neto por Activo")
                r_rows = [{"Activo": k, "Beta": v['beta'], "Delta Puro": v['d_puro'], "Net Delta $": v['d_usd'], "BWD": (v['d_usd']*v['beta'])/data['spy_price']} for k,v in data['risk_map'].items()]
                st.dataframe(pd.DataFrame(r_rows).style.format({"Beta":"{:.2f}", "Net Delta $":"${:,.0f}", "BWD":"{:.2f}"}), use_container_width=True)

            with tab2:
                st.subheader("📈 Contabilidad PMCC")
                df_d = pd.DataFrame(data['detailed'])
                df_opts = df_d[df_d['Type'] == "option"]
                
                # Agrupar por Underlying
                for und, group in df_opts.groupby('Underlying'):
                    longs = group[(group['Qty'] > 0) & (group['Delta'].abs() > 0.60)]
                    shorts = group[(group['Qty'] < 0) & (group['Delta'].abs() < 0.50)]
                    
                    if not longs.empty:
                        # 1. Calcular Realizado (History pairing)
                        realized_pnl = 0
                        closed_trades = []
                        # CORRECCIÓN DE KEYERROR: Solo procesar trades con símbolo
                        valid_hist = [h for h in data['history'] if h.get('type') == 'trade' and 'symbol' in h and get_underlying_symbol(h['symbol']) == und]
                        
                        # Lógica simplificada de emparejamiento para bitácora
                        hist_map = {}
                        for h in valid_hist:
                            s = h['symbol']
                            if s not in hist_map: hist_map[s] = []
                            hist_map[s].append(h)
                        
                        for sym, events in hist_map.items():
                            sto = [e for e in events if e['side'] == 'sell_to_open']
                            btc = [e for e in events if e['side'] == 'buy_to_close']
                            for s, b in zip(sto, btc):
                                pnl = (abs(float(s['price'])) - abs(float(b['price']))) * 100 * abs(float(s['quantity']))
                                realized_pnl += pnl
                                closed_trades.append({"STO": s['date'][:10], "BTC": b['date'][:10], "Strike": sym, "P/L": pnl})

                        # 2. Cálculos Tom King
                        leaps_cost = longs['Cost'].sum()
                        leaps_val = (longs['Price'] * longs['Qty'] * 100).sum()
                        net_income = (leaps_val - leaps_cost) + realized_pnl
                        roi = (net_income / leaps_cost * 100) if leaps_cost > 0 else 0

                        st.markdown(f'<div class="section-header">ACTIVO: {und}</div>', unsafe_allow_html=True)
                        cont1, cont2, cont3, cont4 = st.columns(4)
                        cont1.markdown(f'<div class="pmcc-summary-card"><small>COSTO LEAPS</small><br><b>${leaps_cost:,.0f}</b></div>', unsafe_allow_html=True)
                        cont2.markdown(f'<div class="pmcc-summary-card"><small>CC REALIZADO</small><br><b style="color:#4ade80">${realized_pnl:,.2f}</b></div>', unsafe_allow_html=True)
                        cont3.markdown(f'<div class="pmcc-summary-card"><small>NET INCOME</small><br><b>${net_income:,.0f}</b></div>', unsafe_allow_html=True)
                        cont4.markdown(f'<div class="pmcc-summary-card"><small>ROI</small><br><b style="color:#4ade80">{roi:.1f}%</b></div>', unsafe_allow_html=True)
                        
                        # 3. Mostrar Core Position
                        st.caption("🏛️ Core Position (LEAPS)")
                        st.table(longs[['Exp', 'Strike', 'Qty', 'Cost', 'Delta']])
                        
                        # 4. Monitor de Jugo (si hay short activo)
                        if not shorts.empty:
                            sc = shorts.iloc[0]
                            juice = sc['Extrinsic'] * 100 * abs(sc['Qty'])
                            st.write(f"🥤 **Active Short:** Strike {sc['Strike']} | Exp: {sc['Exp']} | **Jugo: ${juice:.2f}**")
                            if juice < 15: st.error("⚠️ TIEMPO DE ROLEAR (Poco valor extrínseco)")
                        
                        # 5. Bitácora Cerrada
                        if closed_trades:
                            with st.expander("📔 Ver Historial Contable (STO/BTC)"):
                                st.table(pd.DataFrame(closed_trades))
                        st.divider()

            with st.expander("Ver Detalle Crudo de Posiciones"):
                st.dataframe(df_d)
else:
    st.info("👈 Ingresa tu Token para sincronizar.")





