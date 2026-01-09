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
st.set_page_config(layout="wide", page_title="PMCC CEO Command Center", page_icon="🛡️")

# --- DISEÑO UI PREMIUM (CSS) ---
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    /* Tarjetas de KPI */
    .card {
        background-color: #1f2937; 
        padding: 20px; border-radius: 12px; 
        border: 1px solid #374151; text-align: center; height: 140px;
        box-shadow: 2px 2px 10px rgba(0,0,0,0.3);
    }
    .metric-label {color: #9ca3af; font-size: 0.8rem; font-weight: bold; text-transform: uppercase; margin-bottom: 8px;}
    .metric-value {font-size: 1.5rem; font-weight: bold; margin: 0;}
    
    /* Encabezados de Sección */
    .section-header {
        background: linear-gradient(90deg, #238636 0%, #2ea043 100%);
        color: white; padding: 10px 20px; 
        border-radius: 8px; margin: 30px 0 15px 0; font-size: 1.2rem; font-weight: bold;
    }
    .summary-card-pmcc {
        background-color: #161b22; border: 1px solid #30363d;
        padding: 15px; border-radius: 8px; text-align: center; height: 110px;
    }
    .roi-val {font-size: 1.6rem; font-weight: bold;}
    </style>
""", unsafe_allow_html=True)

st.title("🛡️ PMCC CEO Command Center")

# --- INICIALIZAR HISTORIAL ---
if 'history_df' not in st.session_state:
    st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])

# --- SIDEBAR ---
with st.sidebar:
    st.header("📡 Conexión Broker")
    TRADIER_TOKEN = st.text_input("Tradier Access Token", type="password")
    env_mode = st.radio("Entorno", ["Producción (Real)", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción (Real)" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.caption("v17.0.0 | CEO UI Restore")

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
    if df.empty: return pd.Series()
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    col = 'Adj Close' if 'Adj Close' in df.columns else ('Close' if 'Close' in df.columns else df.columns[0])
    series = df[col]
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

# --- MOTOR DE ANÁLISIS ---
def run_master_analysis():
    r_p = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p.status_code != 200: return None
    prof = r_p.json()['profile']['account']
    acct_id = prof['account_number'] if isinstance(prof, dict) else prof[0]['account_number']
    
    r_b = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
    net_liq = float(r_b.json()['balances']['total_equity'])

    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw_pos = r_pos.json().get('positions', {}).get('position', [])
    if not raw_pos or raw_pos == 'null': raw_pos = []
    if isinstance(raw_pos, dict): raw_pos = [raw_pos]

    r_gl = requests.get(f"{BASE_URL}/accounts/{acct_id}/gainloss", headers=get_headers())
    gl_data = r_gl.json().get('gainloss', {}).get('closed_position', [])
    if isinstance(gl_data, dict): gl_data = [gl_data]

    all_syms = list(set(["SPY"] + [p['symbol'] for p in raw_pos] + [get_underlying_symbol(p['symbol']) for p in raw_pos]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}
    spy_p = float(m_map.get('SPY', {}).get('last', 685))

    spy_df_raw = yf.download("SPY", period="1y", progress=False)
    spy_ret = clean_df_finance(spy_df_raw).pct_change().dropna()
    if spy_ret.empty: return None

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
            "Acquired": p.get('date_acquired', 'N/A')[:10], "Last": float(m_d.get('last', 0))
        })

    for s, data in t_map.items():
        total_bwd += (data['d_usd'] * data['beta']) / spy_p if spy_price := spy_p > 0 else 0
        total_exp += abs(data['d_usd'])

    return {
        "nl": net_liq, "rd": total_rd, "bwd": total_bwd, "th": total_th, 
        "lev": total_exp, "risk": t_map, "detailed": detailed_positions, 
        "gl": gl_data, "spy_p": spy_p
    }

# --- UI TABS ---
tab_risk, tab_ceo = st.tabs(["📊 Riesgo & Gráficos", "🏗️ CEO PMCC Accountant"])

if TRADIER_TOKEN:
    if st.button("🚀 ACTUALIZAR COMMAND CENTER"):
        d = run_master_analysis()
        if d:
            # Snapshot Historial
            new_h = {"Timestamp": datetime.now().strftime("%H:%M:%S"), "Net_Liq": d['nl'], "Delta_Neto": d['rd'], "BWD_SPY": d['bwd'], "Theta_Diario": d['th'], "Apalancamiento": d['lev']/d['nl']}
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([new_h])], ignore_index=True)

            with tab_risk:
                st.markdown(f"### 🏦 Balance Neto: ${d['nl']:,.2f}")
                k1, k2, k3, k4 = st.columns(4)
                k1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value">{d["rd"]:.1f}</div><div class="metric-sub">Deltas Puros</div></div>', unsafe_allow_html=True)
                k2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value">{d["bwd"]:.1f}</div><div class="metric-sub">Riesgo Beta-Ajustado</div></div>', unsafe_allow_html=True)
                k3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:#4ade80">${d["th"]:.2f}</div><div class="metric-sub">Income Diario</div></div>', unsafe_allow_html=True)
                k4.markdown(f'<div class="card"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value">{d["lev"]/d["nl"]:.2f}x</div><div class="metric-sub">Nocional / Cash</div></div>', unsafe_allow_html=True)

                st.divider()
                h = st.session_state.history_df
                if len(h) > 1:
                    g1, g2 = st.columns(2)
                    with g1: st.write("**Capital ($)**"); st.area_chart(h, x="Timestamp", y="Net_Liq")
                    with g2: st.write("**Riesgo BWD**"); st.line_chart(h, x="Timestamp", y="BWD_SPY")

            with tab_ceo:
                st.subheader("📋 Contabilidad Forense de Campañas PMCC")
                df_det = pd.DataFrame(d['detailed'])
                
                for und, group in df_det[df_det['Type'] == "option"].groupby('Underlying'):
                    longs = group[(group['Qty'] > 0) & (group['Delta'].abs() > 0.55)]
                    if not longs.empty:
                        start_date = datetime.strptime(longs['Acquired'].min(), '%Y-%m-%d')
                        realized_income = 0
                        closed_list = []
                        for gl in d['gl']:
                            u_sym, o_type, strike = decode_occ_symbol(gl.get('symbol',''))
                            if u_sym == und and o_type == "CALL":
                                close_dt = datetime.strptime(gl.get('close_date','2000-01-01')[:10], '%Y-%m-%d')
                                if close_dt >= start_date:
                                    is_leap_strike = any(abs(strike - ls) < 0.5 for ls in longs['Strike'])
                                    if not is_leap_strike:
                                        gain = float(gl.get('gain_loss', 0))
                                        realized_income += gain
                                        closed_list.append({"Fecha": close_dt.strftime('%Y-%m-%d'), "Strike": strike, "P/L": gain})

                        l_cost = longs['Cost'].sum()
                        l_val = (longs['Last'] * longs['Qty'] * 100).sum()
                        net_inc = (l_val - l_cost) + realized_income
                        roi = (net_inc / l_cost * 100) if l_cost > 0 else 0

                        st.markdown(f'<div class="section-header">SYMBOL: {und} (Spot: ${group["Price"].iloc[0]:.2f})</div>', unsafe_allow_html=True)
                        
                        # --- LAS 5 TARJETAS RECUPERADAS ---
                        cc1, cc2, cc3, cc4, cc5 = st.columns(5)
                        cc1.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${l_cost:,.2f}</p></div>', unsafe_allow_html=True)
                        cc2.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${l_val:,.2f}</p></div>', unsafe_allow_html=True)
                        cc3.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${realized_income:,.2f}</p></div>', unsafe_allow_html=True)
                        cc4.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${net_inc:,.2f}</p></div>', unsafe_allow_html=True)
                        
                        roi_col = "#4ade80" if roi >= 0 else "#f87171"
                        cc5.markdown(f'<div class="summary-card-pmcc"><p class="kpi-label">ROI TOTAL</p><p class="roi-val" style="color:{roi_col}">{roi:.1f}%</p></div>', unsafe_allow_html=True)
                        
                        st.write("### 🏛️ CORE POSITION (LEAPS)")
                        st.table(longs[['Exp', 'Strike', 'Qty', 'Delta']])
                        
                        shorts = group[(group['Qty'] < 0) & (group['Delta'].abs() < 0.50)]
                        if not shorts.empty:
                            sc = shorts.iloc[0]
                            j = sc['Extrinsic'] * 100 * abs(sc['Qty'])
                            st.write(f"🥤 **Active Short:** K {sc['Strike']} | Exp: {sc['Exp']} | **Jugo: ${j:.2f}**")
                            if j < 15: st.error("🚨 TIEMPO DE ROLEAR")
                        
                        if closed_list:
                            with st.expander("📔 Ver Historial Filtrado"): st.table(pd.DataFrame(closed_list))
                        st.divider()

            with st.expander("Ver Detalle Crudo"): st.dataframe(df_det)
else:
    st.info("👈 Ingresa tu Token de Tradier.")

