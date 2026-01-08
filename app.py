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
st.set_page_config(layout="wide", page_title="Risk & Greeks Commander Pro", page_icon="🛡️")

# Estilos CSS
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .card {
        background-color: #1f2937; 
        padding: 20px; 
        border-radius: 10px; 
        border: 1px solid #374151; 
        text-align: center;
        height: 100%;
    }
    .metric-label {color: #aaa; font-size: 0.9rem; font-weight: bold; margin-bottom: 5px;}
    .metric-value {font-size: 1.8rem; font-weight: bold; margin: 0;}
    .metric-sub {font-size: 0.8rem; color: #888; margin-top: 5px;}
    </style>
""", unsafe_allow_html=True)

st.title("🛡️ Portfolio Intelligence Command Center")

# --- INICIALIZAR HISTORIAL ---
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
    if st.button("🗑️ Reiniciar Sesión"):
        st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])
        st.rerun()
    st.caption("v9.2.0 | Full History Mode")

# --- FUNCIONES ---
def map_to_yahoo(symbol):
    s = symbol.upper().strip()
    if s in ['SPX', 'SPXW', 'SPX.X']: return '^SPX'
    if s in ['NDX', 'NDXW', 'NDX.X']: return '^NDX'
    if s in ['RUT', 'RUTW', 'RUT.X']: return '^RUT'
    return s.replace('/', '-')

def get_underlying_symbol(symbol):
    if len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def get_headers():
    return {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

@st.cache_data(ttl=3600)
def calculate_beta(ticker, spy_returns):
    if ticker in ['BIL', 'SGOV', 'SHV', 'USFR']: return 0.0
    try:
        stock = yf.download(map_to_yahoo(ticker), period="1y", progress=False)
        stock.index = stock.index.tz_localize(None)
        col = 'Adj Close' if 'Adj Close' in stock.columns else 'Close'
        ret = stock[col].pct_change().dropna()
        aligned = pd.concat([ret, spy_returns], axis=1, join='inner').dropna()
        return aligned.iloc[:,0].cov(aligned.iloc[:,1]) / aligned.iloc[:,1].var()
    except: return 1.0

# --- CÁLCULOS ---
def run_analysis():
    # Perfil y Balance
    r_p = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p.status_code != 200: return None
    acct = r_p.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']
    r_b = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
    net_liq = float(r_b.json()['balances']['total_equity'])

    # Posiciones
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw = r_pos.json().get('positions', {}).get('position', [])
    if isinstance(raw, dict): raw = [raw]
    if not raw: return net_liq, 0, 0, 0, 0, {}

    # Market Data
    syms = ["SPY"] + [p['symbol'] for p in raw] + [get_underlying_symbol(p['symbol']) for p in raw]
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(list(set(syms))), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])}
    spy_p = float(m_map.get('SPY', {}).get('last', 0))

    # Yahoo Data
    spy_df = yf.download("SPY", period="1y", progress=False)
    spy_ret = (spy_df['Adj Close'] if 'Adj Close' in spy_df.columns else spy_df['Close']).tz_localize(None).pct_change().dropna()
    
    # Greeks & Netting
    rd_tot, th_tot, exp_tot, bwd_tot = 0, 0, 0, 0
    t_map = {}
    
    for p in raw:
        qty = float(p['quantity'])
        u_sym = get_underlying_symbol(p['symbol'])
        m_d = m_map.get(p['symbol'], {})
        u_p = float(m_map.get(u_sym, {}).get('last', 0))
        mult = 100 if len(p['symbol']) > 5 else 1
        
        d = float(m_d.get('greeks', {}).get('delta', 1.0 if mult==1 else 0))
        th = float(m_d.get('greeks', {}).get('theta', 0))
        
        if u_sym not in t_map:
            t_map[u_sym] = {'d_usd': 0, 'th_usd': 0, 'beta': calculate_beta(u_sym, spy_ret), 'price': u_p}
        
        t_map[u_sym]['d_usd'] += (qty * d * mult * u_p)
        t_map[u_sym]['th_usd'] += (qty * th * mult)
        rd_tot += (qty * d * mult)
        th_tot += (qty * th * mult)

    for s, data in t_map.items():
        bwd_tot += (data['d_usd'] * data['beta']) / spy_p if spy_p > 0 else 0
        exp_tot += abs(data['d_usd'])

    return net_liq, rd_tot, bwd_tot, th_tot, exp_tot/net_liq, t_map

# --- UI ---
if TRADIER_TOKEN:
    if st.button("🔄 ACTUALIZAR DASHBOARD"):
        res = run_analysis()
        if res:
            nl, rd, bwd, th, lev, r_map = res
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([{
                "Timestamp": datetime.now().strftime("%H:%M:%S"),
                "Net_Liq": nl, "Delta_Neto": rd, "BWD_SPY": bwd, "Theta_Diario": th, "Apalancamiento": lev
            }])], ignore_index=True)

            # --- HEADER ---
            st.markdown(f"### 🏦 Balance Neto: ${nl:,.2f}")
            c1, c2, c3, c4 = st.columns(4)
            colors = ["#4ade80" if x >= 0 else "#f87171" for x in [rd, bwd, th]]
            c1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value" style="color:{colors[0]}">{rd:.1f}</div></div>', unsafe_allow_html=True)
            c2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value" style="color:{colors[1]}">{bwd:.1f}</div></div>', unsafe_allow_html=True)
            c3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:{colors[2]}">${th:.2f}</div></div>', unsafe_allow_html=True)
            c4.markdown(f'<div class="card" style="border-bottom:5px solid {"#4ade80" if lev < 1.5 else "#facc15"}"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value">{lev:.2f}x</div></div>', unsafe_allow_html=True)

            # --- HISTORIAL COMPLETO ---
            st.divider()
            st.subheader("📈 Comportamiento de la Sesión")
            h = st.session_state.history_df
            
            if len(h) > 1:
                # Fila 1: Balance y Riesgo SPY
                row1_col1, row1_col2 = st.columns(2)
                with row1_col1:
                    st.caption("💰 Evolución Balance Neto")
                    st.area_chart(h, x="Timestamp", y="Net_Liq", color="#00d4ff")
                with row1_col2:
                    st.caption("⚖️ Evolución BWD (Riesgo SPY)")
                    st.line_chart(h, x="Timestamp", y="BWD_SPY", color="#4ade80")

                # Fila 2: Delta, Theta y Apalancamiento
                row2_col1, row2_col2, row2_col3 = st.columns(3)
                with row2_col1:
                    st.caption("🎯 Delta Neto (Pure)")
                    st.line_chart(h, x="Timestamp", y="Delta_Neto")
                with row2_col2:
                    st.caption("⏳ Theta (Renta)")
                    st.line_chart(h, x="Timestamp", y="Theta_Diario")
                with row2_col3:
                    st.caption("🚀 Apalancamiento")
                    st.line_chart(h, x="Timestamp", y="Apalancamiento")
                
                csv = h.to_csv(index=False).encode('utf-8')
                st.download_button("💾 Exportar Datos (.csv)", csv, f"session_{datetime.now().strftime('%H%M')}.csv")
            
            # --- TABLA DE ACTIVOS ---
            st.divider()
            st.subheader("Riesgo por Activo")
            df_assets = pd.DataFrame([{"Activo": k, "Beta": f"{v['beta']:.2f}", "Delta $": f"{v['delta_usd']:,.0f}", "Theta $": f"{v['th_usd']:.2f}"} for k,v in r_map.items()])
            st.table(df_assets)
else:
    st.info("👈 Ingresa tu Token para iniciar.")


