import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import re
import time
import plotly.graph_objects as go
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="Trading Command Center Pro", page_icon="🛡️")

# --- DISEÑO UI PREMIUM (CSS) ---
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .card {
        background-color: #1f2937; 
        padding: 20px; border-radius: 12px; 
        border: 1px solid #374151; text-align: center; height: 140px;
        box-shadow: 2px 2px 10px rgba(0,0,0,0.3);
    }
    .metric-label {color: #9ca3af; font-size: 0.85rem; font-weight: bold; text-transform: uppercase; margin-bottom: 8px;}
    .metric-value {font-size: 2rem; font-weight: bold; margin: 0;}
    .metric-sub {font-size: 0.8rem; color: #6b7280; margin-top: 5px;}
    .section-header {
        background: linear-gradient(90deg, #238636 0%, #2ea043 100%);
        color: white; padding: 10px 20px; 
        border-radius: 8px; margin: 30px 0 15px 0; font-size: 1.2rem; font-weight: bold;
    }
    .pmcc-summary-card {
        background-color: #161b22; border: 1px solid #30363d;
        padding: 10px; border-radius: 8px; text-align: center;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🛡️ Trading Command Center Pro")

# --- INICIALIZAR HISTORIAL EN MEMORIA ---
if 'history_df' not in st.session_state:
    st.session_state.history_df = pd.DataFrame(columns=[
        "Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"
    ])

# --- SIDEBAR ---
with st.sidebar:
    st.header("📡 Conexión Broker")
    TRADIER_TOKEN = st.text_input("Tradier Access Token", type="password")
    env_mode = st.radio("Entorno", ["Producción (Real)", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción (Real)" else "https://sandbox.tradier.com/v1"
    st.divider()
    if st.button("🗑️ Limpiar Gráficos"):
        st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])
        st.rerun()
    st.caption("v16.1.0 | Yahoo Data Fix")

# --- FUNCIONES DE APOYO ---
def get_headers(): return {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

def map_to_yahoo(symbol):
    s = symbol.upper().strip()
    if s in ['SPX', 'SPXW']: return '^GSPC'
    if s in ['NDX', 'NDXW']: return '^NDX'
    if s in ['RUT', 'RUTW']: return '^RUT'
    return s.replace('/', '-')

def get_underlying_symbol(symbol):
    if len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def decode_occ_symbol(symbol):
    if not symbol or len(symbol) < 15: return symbol, "STOCK", 0
    try:
        match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
        if match:
            o_type = "CALL" if match.group(3) == "C" else "PUT"
            strike = float(match.group(4)) / 1000
            return match.group(1), o_type, strike
    except: pass
    return symbol, "UNKNOWN", 0

def clean_df_finance(df):
    """Extrae la serie de precios de forma robusta de un dataframe de yfinance"""
    if df.empty: return pd.Series()
    if isinstance(df.columns, pd.MultiIndex):
        # Aplanar MultiIndex (común en versiones nuevas de yfinance)
        df.columns = df.columns.get_level_values(0)
    
    # Intentar obtener Adj Close, si no Close
    if 'Adj Close' in df.columns:
        series = df['Adj Close']
    elif 'Close' in df.columns:
        series = df['Close']
    else:
        series = df.iloc[:, 0] # Tomar la primera columna disponible
        
    series.index = series.index.tz_localize(None)
    return series

@st.cache_data(ttl=3600)
def get_beta(ticker, spy_returns):
    if ticker in ['BIL', 'SGOV', 'SHV']: return 0.0
    try:
        data = yf.download(map_to_yahoo(ticker), period="1y", progress=False)
        stock_series = clean_df_finance(data)
        if stock_series.empty: return 1.0
        
        ret = stock_series.pct_change().dropna()
        aligned = pd.concat([ret, spy_returns], axis=1, join='inner').dropna()
        if len(aligned) < 10: return 1.0
        return aligned.iloc[:,0].cov(aligned.iloc[:,1]) / aligned.iloc[:,1].var()
    except: return 1.0

# --- EL MOTOR DE ANÁLISIS ---
def run_master_analysis():
    # 1. Identificar Cuenta
    r_p = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p.status_code != 200: return None
    acct_data = r_p.json()['profile']['account']
    acct_id = acct_data[0]['account_number'] if isinstance(acct_data, list) else acct_data['account_number']
    
    r_b = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
    net_liq = float(r_b.json()['balances']['total_equity'])

    # 2. Posiciones Abiertas
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw_pos = r_pos.json().get('positions', {}).get('position', [])
    if not raw_pos or raw_pos == 'null': raw_pos = []
    if isinstance(raw_pos, dict): raw_pos = [raw_pos]

    # 3. Ganancias Realizadas
    r_gl = requests.get(f"{BASE_URL}/accounts/{acct_id}/gainloss", headers=get_headers())
    gl_data = r_gl.json().get('gainloss', {}).get('closed_position', [])
    if isinstance(gl_data, dict): gl_data = [gl_data]

    # 4. Market Data (Tradier)
    all_syms = list(set(["SPY"] + [p['symbol'] for p in raw_pos] + [get_underlying_symbol(p['symbol']) for p in raw_pos]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}
    spy_p = float(m_map.get('SPY', {}).get('last', 685))

    # 5. Yahoo para Beta (Robusto)
    spy_df_raw = yf.download("SPY", period="1y", progress=False)
    spy_ret = clean_df_finance(spy_df_raw).pct_change().dropna()
    if spy_ret.empty:
        st.error("Error al obtener datos de referencia de Yahoo Finance. Reintenta en 1 minuto.")
        return None

    # 6. Procesamiento
    total_rd, total_th, total_exp, total_bwd = 0, 0, 0, 0
    t_map = {}
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
        ex = float(m_d.get('greeks', {}).get('extrinsic', 0))
        
        if u_sym not in t_map:
            t_map[u_sym] = {'d_usd': 0, 'th_usd': 0, 'd_puro': 0, 'beta': get_beta(u_sym, spy_ret), 'price': u_p}
        
        t_map[u_sym]['d_usd'] += (qty * d * mult * u_p)
        t_map[u_sym]['th_usd'] += (qty * th * mult)
        t_map[u_sym]['d_puro'] += (qty * d * mult)
        total_rd += (qty * d * mult)
        total_th += (qty * th * mult)
        
        detailed_positions.append({
            "Symbol": sym, "Qty": qty, "Underlying": u_sym, "Type": "option" if is_opt else "stock",
            "Delta": d, "Theta": th, "Price": u_p, "Extrinsic": ex, "Strike": m_d.get('strike', 0),
            "Exp": m_d.get('expiration_date', 'N/A'), "Cost": abs(float(p.get('cost_basis', 0))),
            "Acquired": p.get('date_acquired', 'N/A')[:10]
        })

    for s, data in t_map.items():
        total_bwd += (data['d_usd'] * data['beta']) / spy_p if spy_p > 0 else 0
        total_exp += abs(data['d_usd'])

    return {
        "nl": net_liq, "rd": total_rd, "bwd": total_bwd, "th": total_th, 
        "lev": total_exp, "risk": t_map, "detailed": detailed_positions, 
        "gl": gl_data, "spy_p": spy_p
    }

# --- UI TABS ---
tab_risk, tab_acc = st.tabs(["📊 Riesgo & Gráficos", "🏗️ PMCC Accountant (Tom King)"])

if TRADIER_TOKEN:
    if st.button("🚀 ACTUALIZAR TODO EL COMANDO"):
        d = run_master_analysis()
        if d:
            # Snapshot Historial
            new_h = {"Timestamp": datetime.now().strftime("%H:%M:%S"), "Net_Liq": d['nl'], "Delta_Neto": d['rd'], "BWD_SPY": d['bwd'], "Theta_Diario": d['th'], "Apalancamiento": d['lev']/d['nl']}
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([new_h])], ignore_index=True)

            with tab_risk:
                st.markdown(f"### 🏦 Balance Neto: ${d['nl']:,.2f}")
                k1, k2, k3, k4 = st.columns(4)
                c_rd = "#4ade80" if d['rd'] > 0 else "#f87171"
                c_bwd = "#4ade80" if d['bwd'] > 0 else "#f87171"
                c_th = "#4ade80" if d['th'] > 0 else "#f87171"
                
                k1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value" style="color:{c_rd}">{d["rd"]:.1f}</div><div class="metric-sub">Deltas Puros</div></div>', unsafe_allow_html=True)
                k2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value" style="color:{c_bwd}">{d["bwd"]:.1f}</div><div class="metric-sub">Riesgo Beta-Ajustado</div></div>', unsafe_allow_html=True)
                k3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:{c_th}">${d["th"]:.2f}</div><div class="metric-sub">Ingreso por Tiempo</div></div>', unsafe_allow_html=True)
                k4.markdown(f'<div class="card" style="border-bottom: 5px solid #00d4ff"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value">{d["lev"]/d["nl"]:.2f}x</div><div class="metric-sub">Nocional / Cash</div></div>', unsafe_allow_html=True)

                st.divider()
                h = st.session_state.history_df
                if len(h) > 1:
                    st.subheader("📈 Tendencias de la Sesión")
                    g1, g2 = st.columns(2)
                    g1.write("**Evolución Capital**"); g1.area_chart(h, x="Timestamp", y="Net_Liq")
                    g2.write("**Riesgo BWD**"); g2.line_chart(h, x="Timestamp", y="BWD_SPY")

                st.subheader("📊 Riesgo por Activo")
                r_rows = [{"Activo": k, "Beta": v['beta'], "Delta Puro": v['d_puro'], "Net Delta $": v['d_usd'], "BWD": (v['d_usd']*v['beta'])/d['spy_p']} for k,v in d['risk'].items()]
                st.dataframe(pd.DataFrame(r_rows).sort_values(by='BWD', ascending=False), use_container_width=True)

            with tab_acc:
                st.subheader("🏗️ Contabilidad de Campañas PMCC")
                df_det = pd.DataFrame(d['detailed'])
                for und, group in df_det[df_det['Type'] == "option"].groupby('Underlying'):
                    longs = group[(group['Qty'] > 0) & (group['Delta'].abs() > 0.55)]
                    if not longs.empty:
                        start_date = datetime.strptime(longs['Acquired'].min(), '%Y-%m-%d')
                        realized_income = 0
                        closed_trades = []
                        for gl in d['gl']:
                            u_sym, o_type, strike = decode_occ_symbol(gl.get('symbol',''))
                            if u_sym == und and o_type == "CALL":
                                close_dt = datetime.strptime(gl.get('close_date','2000-01-01')[:10], '%Y-%m-%d')
                                if close_dt >= start_date:
                                    is_leap_strike = any(abs(strike - ls) < 0.5 for ls in longs['Strike'])
                                    if not is_leap_strike:
                                        gain = float(gl.get('gain_loss', 0))
                                        realized_income += gain
                                        closed_trades.append({"Fecha": close_dt.strftime('%Y-%m-%d'), "Strike": strike, "P/L": gain})

                        l_cost = longs['Cost'].sum()
                        l_val = (longs['Price'] * longs['Qty'] * 100).sum()
                        net_inc = (l_val - l_cost) + realized_income
                        st.markdown(f'<div class="section-header">ACTIVO: {und}</div>', unsafe_allow_html=True)
                        cc1, cc2, cc3 = st.columns(3)
                        cc1.markdown(f'<div class="pmcc-summary-card"><small>CC REALIZADO</small><br><b style="color:#4ade80">${realized_income:,.2f}</b></div>', unsafe_allow_html=True)
                        cc2.markdown(f'<div class="pmcc-summary-card"><small>NET INCOME</small><br><b>${net_inc:,.2f}</b></div>', unsafe_allow_html=True)
                        cc3.markdown(f'<div class="pmcc-summary-card"><small>ROI</small><br><b style="color:#4ade80">{(net_inc/l_cost*100) if l_cost > 0 else 0:.1f}%</b></div>', unsafe_allow_html=True)
                        
                        st.caption("🏛️ Core Positions (LEAPS)")
                        st.table(longs[['Exp', 'Strike', 'Qty', 'Delta']])
                        
                        shorts = group[(group['Qty'] < 0) & (group['Delta'].abs() < 0.50)]
                        if not shorts.empty:
                            sc = shorts.iloc[0]
                            j = sc['Extrinsic'] * 100 * abs(sc['Qty'])
                            st.write(f"🥤 **Short Activo:** K {sc['Strike']} | **Jugo: ${j:.2f}**")
                        if closed_trades:
                            with st.expander("📔 Ver Historial Cerrado"): st.table(pd.DataFrame(closed_trades))
                        st.divider()

            with st.expander("Ver Detalle Crudo"): st.dataframe(df_det)
else:
    st.info("👈 Ingresa tu Token de Tradier.")






