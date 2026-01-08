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
st.set_page_config(layout="wide", page_title="Risk & PMCC Commander", page_icon="🛡️")

# Estilos CSS Profesionales
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .card {
        background-color: #1f2937; 
        padding: 20px; border-radius: 10px; 
        border: 1px solid #374151; text-align: center; height: 100%;
    }
    .metric-label {color: #aaa; font-size: 0.9rem; font-weight: bold;}
    .metric-value {font-size: 1.8rem; font-weight: bold; margin: 0;}
    .pmcc-header {background-color: #161b22; padding: 10px; border-radius: 5px; border-left: 5px solid #00d4ff; margin-bottom: 10px;}
    </style>
""", unsafe_allow_html=True)

st.title("🛡️ Portfolio Intelligence & PMCC Center")

# --- INICIALIZAR HISTORIAL EN MEMORIA ---
if 'history_df' not in st.session_state:
    st.session_state.history_df = pd.DataFrame(columns=[
        "Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"
    ])

# --- SIDEBAR: CONEXIÓN ---
with st.sidebar:
    st.header("📡 Conexión Broker")
    TRADIER_TOKEN = st.text_input("Tradier Access Token", type="password")
    env_mode = st.radio("Entorno", ["Producción (Real)", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción (Real)" else "https://sandbox.tradier.com/v1"
    st.divider()
    if st.button("🗑️ Reiniciar Sesión"):
        st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])
        st.rerun()
    st.caption("v10.0.0 | Multi-Module Active")

# --- FUNCIONES DE APOYO (MECÁNICA) ---
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

# --- MOTOR DE BETA ---
def clean_data(df):
    if isinstance(df, pd.Series): df = df.to_frame()
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    col = 'Adj Close' if 'Adj Close' in df.columns else 'Close'
    df = df[[col]].copy()
    df.index = df.index.tz_localize(None)
    return df

@st.cache_data(ttl=3600)
def calculate_beta_individual(ticker, spy_returns):
    if ticker in ['BIL', 'SGOV', 'SHV', 'USFR']: return 0.0
    yahoo_sym = map_to_yahoo(ticker)
    try:
        stock_raw = yf.download(yahoo_sym, period="1y", progress=False)
        if stock_raw.empty: return 1.0
        stock_returns = clean_data(stock_raw).pct_change().dropna()
        aligned = pd.concat([stock_returns, spy_returns], axis=1, join='inner').dropna()
        return aligned.iloc[:,0].cov(aligned.iloc[:,1]) / aligned.iloc[:,1].var()
    except: return 1.0

# --- LÓGICA DE DATOS ---
def run_analysis():
    # 1. Cuenta y Balance
    r_p = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p.status_code != 200: return None
    acct = r_p.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']
    r_b = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
    net_liq = float(r_b.json()['balances']['total_equity'])

    # 2. Posiciones
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw = r_pos.json().get('positions', {}).get('position', [])
    if raw is None or raw == 'null': return net_liq, 0, 0, 0, 0, {}, [], 1.0
    if isinstance(raw, dict): raw = [raw]

    # 3. Market Data (Tradier)
    syms = ["SPY"] + [p['symbol'] for p in raw] + [get_underlying_symbol(p['symbol']) for p in raw]
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(list(set(syms))), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])}
    spy_p = float(m_map.get('SPY', {}).get('last', 685))

    # 4. Yahoo Data para Betas
    spy_df = yf.download("SPY", period="1y", progress=False)
    spy_p_yf = spy_df['Adj Close'] if 'Adj Close' in spy_df.columns else spy_df['Close']
    spy_ret = spy_p_yf.tz_localize(None).pct_change().dropna()
    
    # 5. Greeks & Netting
    total_raw_delta, total_theta, total_abs_exp, total_bwd = 0, 0, 0, 0
    t_map = {}
    detailed_list = []
    
    for p in raw:
        qty = float(p['quantity'])
        u_sym = get_underlying_symbol(p['symbol'])
        m_d = m_map.get(p['symbol'], {})
        u_p = float(m_map.get(u_sym, {}).get('last', 0))
        is_option = len(p['symbol']) > 5
        mult = 100 if is_option else 1
        
        d = float(m_d.get('greeks', {}).get('delta', 1.0 if not is_option else 0))
        th = float(m_d.get('greeks', {}).get('theta', 0))
        ex = float(m_d.get('greeks', {}).get('extrinsic', 0))
        
        if u_sym not in t_map:
            t_map[u_sym] = {'d_usd': 0, 'th_usd': 0, 'd_puro': 0, 'beta': calculate_beta_individual(u_sym, spy_ret), 'price': u_p}
        
        t_map[u_sym]['d_usd'] += (qty * d * mult * u_p)
        t_map[u_sym]['th_usd'] += (qty * th * mult)
        t_map[u_sym]['d_puro'] += (qty * d * mult)
        total_raw_delta += (qty * d * mult)
        total_theta += (qty * th * mult)
        
        detailed_list.append({
            "Símbolo": p['symbol'], "Tipo": "Opción" if is_option else "Stock", 
            "Qty": qty, "Underlying": u_sym, "Delta": d, "Theta": th, 
            "Price": u_p, "Extrinsic": ex, "Exp": m_d.get('expiration_date', 'N/A'),
            "Strike": m_d.get('strike', 0)
        })

    for s, data in t_map.items():
        total_bwd += (data['d_usd'] * data['beta']) / spy_p if spy_p > 0 else 0
        total_abs_exp += abs(data['d_usd'])

    return net_liq, total_raw_delta, total_bwd, total_theta, total_abs_exp/net_liq, t_map, detailed_list, spy_p

# --- UI TABS ---
tab_risk, tab_pmcc = st.tabs(["📊 Riesgo & Historial", "🏗️ PMCC Commander"])

if TRADIER_TOKEN:
    if st.button("🚀 ACTUALIZAR TODO"):
        res = run_analysis()
        if res:
            nl, rd, bwd, th, lev, r_map, d_list, spy_price = res
            
            # Grabar Historial
            new_entry = {"Timestamp": datetime.now().strftime("%H:%M:%S"), "Net_Liq": nl, "Delta_Neto": rd, "BWD_SPY": bwd, "Theta_Diario": th, "Apalancamiento": lev}
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([new_entry])], ignore_index=True)

            with tab_risk:
                st.markdown(f"### 🏦 Balance Neto: ${nl:,.2f}")
                col1, col2, col3, col4 = st.columns(4)
                c_rd = "#4ade80" if rd > 0 else "#f87171"
                c_bwd = "#4ade80" if bwd > 0 else "#f87171"
                c_th = "#4ade80" if th > 0 else "#f87171"
                c_lev = "#4ade80" if lev < 1.5 else "#facc15"
                
                col1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value" style="color:{c_rd}">{rd:.1f}</div></div>', unsafe_allow_html=True)
                col2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value" style="color:{c_bwd}">{bwd:.1f}</div></div>', unsafe_allow_html=True)
                col3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:{c_th}">${th:.2f}</div></div>', unsafe_allow_html=True)
                col4.markdown(f'<div class="card" style="border-bottom:5px solid {c_lev}"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value">{lev:.2f}x</div></div>', unsafe_allow_html=True)

                # Gráficos de Historial
                st.divider()
                h = st.session_state.history_df
                if len(h) > 1:
                    st.subheader("📈 Tendencia de la Sesión")
                    g1, g2 = st.columns(2)
                    with g1: st.area_chart(h, x="Timestamp", y="Net_Liq", title="Capital")
                    with g2: st.line_chart(h, x="Timestamp", y="BWD_SPY", title="Riesgo SPY")

                # Tabla Agrupada
                st.subheader("📊 Riesgo Neto por Activo")
                rows = [{"Activo": k, "Beta": v['beta'], "Delta Puro": v['d_puro'], "Net Delta $": v['d_usd'], "Net Theta $": v['th_usd']} for k,v in r_map.items()]
                st.dataframe(pd.DataFrame(rows).style.format({"Beta": "{:.2f}", "Net Delta $": "${:,.0f}", "Net Theta $": "${:,.2f}"}), use_container_width=True)

            with tab_pmcc:
                st.subheader("🏗️ Monitor de Campañas PMCC")
                df_opts = pd.DataFrame(d_list)
                df_opts = df_opts[df_opts['Type'] == "Option"]
                
                if not df_opts.empty:
                    for und, group in df_opts.groupby('Underlying'):
                        long_c = group[(group['Qty'] > 0) & (group['Delta'] > 0.60)]
                        short_c = group[(group['Qty'] < 0) & (group['Delta'] < 0.45)]
                        
                        if not long_c.empty and not short_c.empty:
                            lc, sc = long_c.iloc[0], short_c.iloc[0]
                            st.markdown(f'<div class="pmcc-header">🚀 Activo: <b>{und}</b></div>', unsafe_allow_html=True)
                            p1, p2, p3, p4 = st.columns(4)
                            p1.write(f"**LEAPS**: K {lc['Strike']} | D: {lc['Delta']:.2f}")
                            p2.write(f"**SHORT**: K {sc['Strike']} | D: {sc['Delta']:.2f}")
                            juice = sc['Extrinsic'] * 100 * abs(sc['Qty'])
                            p3.write(f"**JUGO**: ${juice:.2f}")
                            if juice < 10: st.warning("🪫 Poco extrínseco.")
                            p4.write(f"**SALUD**: {(lc['Delta']/abs(sc['Delta'])):.1f}x L/S")
                            st.divider()
                else:
                    st.info("No se detectaron estructuras PMCC en la cartera.")
else:
    st.info("👈 Ingresa tu Token en la barra lateral.")


