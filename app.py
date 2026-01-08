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
st.set_page_config(layout="wide", page_title="Risk & Greeks Commander", page_icon="📈")

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

st.title("🛡️ Portfolio Intelligence Dashboard")

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
    if st.button("🗑️ Borrar Historial de Sesión"):
        st.session_state.history_df = pd.DataFrame(columns=["Timestamp", "Net_Liq", "Delta_Neto", "BWD_SPY", "Theta_Diario", "Apalancamiento"])
        st.rerun()

# --- FUNCIONES DE APOYO ---
def map_to_yahoo(symbol):
    s = symbol.upper().strip()
    if s in ['SPX', 'SPXW', 'SPX.X']: return '^SPX'
    if s in ['NDX', 'NDXW', 'NDX.X']: return '^NDX'
    if s in ['RUT', 'RUTW', 'RUT.X']: return '^RUT'
    if s in ['VIX', 'VIX.X']: return '^VIX'
    return s.replace('/', '-')

def get_underlying_symbol(symbol):
    if len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def get_headers():
    return {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

@st.cache_data(ttl=3600)
def calculate_beta_individual(ticker, spy_returns):
    if ticker in ['BIL', 'SGOV', 'SHV', 'USFR']: return 0.0
    try:
        stock_raw = yf.download(map_to_yahoo(ticker), period="1y", progress=False)
        if stock_raw.empty: return 1.0
        # Limpieza de Timezone
        stock_raw.index = stock_raw.index.tz_localize(None)
        
        # Manejo de columnas para yfinance nuevo
        col = 'Adj Close' if 'Adj Close' in stock_raw.columns else 'Close'
        stock_ret = stock_raw[col].pct_change().dropna()
        
        aligned = pd.concat([stock_ret, spy_returns], axis=1, join='inner').dropna()
        return aligned.iloc[:,0].cov(aligned.iloc[:,1]) / aligned.iloc[:,1].var()
    except: return 1.0

# --- LÓGICA DE DATOS ---

def run_analysis():
    # 1. Obtener Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']
    
    r_bal = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
    net_liq = float(r_bal.json()['balances']['total_equity'])

    # 2. Obtener Posiciones
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    raw_pos = r_pos.json().get('positions', {}).get('position', [])
    if isinstance(raw_pos, dict): raw_pos = [raw_pos]
    if not raw_pos: return net_liq, 0, 0, 0, 0, {}
    
    symbols_to_fetch = ["SPY"]
    portfolio = []
    for p in raw_pos:
        u_sym = get_underlying_symbol(p['symbol'])
        portfolio.append({"Symbol": p['symbol'], "Qty": float(p['quantity']), "Underlying": u_sym, "Type": "Option" if len(p['symbol']) > 5 else "Stock"})
        symbols_to_fetch.extend([p['symbol'], u_sym])

    # 3. Datos de Mercado (Tradier)
    unique_syms = ",".join(list(set(symbols_to_fetch)))
    r_qs = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': unique_syms, 'greeks': 'true'}, headers=get_headers())
    quotes = r_qs.json().get('quotes', {}).get('quote', [])
    if isinstance(quotes, dict): quotes = [quotes]
    
    market_map = {q['symbol']: q for q in quotes}
    spy_price = float(market_map.get('SPY', {}).get('last', 0))

    # 4. Datos de Yahoo (SPY una sola vez)
    spy_df = yf.download("SPY", period="1y", progress=False)
    if spy_df.empty:
        st.error("Yahoo Finance bloqueó la petición (Rate Limit). Reintenta en 1 minuto.")
        st.stop()
    
    # Extraer precios de SPY correctamente
    spy_prices = spy_df['Adj Close'] if 'Adj Close' in spy_df.columns else spy_df['Close']
    spy_prices.index = spy_prices.index.tz_localize(None)
    spy_returns = spy_prices.pct_change().dropna()
    
    # Calcular Betas
    underlyings = list(set([p['Underlying'] for p in portfolio]))
    betas = {t: calculate_beta_individual(t, spy_returns) for t in underlyings}

    # 5. Cálculos
    total_raw_delta = 0
    total_theta = 0
    ticker_risk = {}

    for p in portfolio:
        m_data = market_map.get(p['Symbol'], {})
        u_price = float(market_map.get(p['Underlying'], {}).get('last', 0))
        delta = float(m_data.get('greeks', {}).get('delta', 1.0 if p['Type']=="Stock" else 0))
        theta = float(m_data.get('greeks', {}).get('theta', 0))
        
        mult = 100 if p['Type'] == "Option" else 1
        pos_raw_delta = p['Qty'] * delta * mult
        pos_delta_dollars = pos_raw_delta * u_price
        pos_theta_dollars = p['Qty'] * theta * mult
        
        total_raw_delta += pos_raw_delta
        total_theta += pos_theta_dollars
        
        if p['Underlying'] not in ticker_risk:
            ticker_risk[p['Underlying']] = {'delta_usd': 0, 'theta_usd': 0, 'beta': betas.get(p['Underlying'], 1.0), 'price': u_price}
        ticker_risk[p['Underlying']]['delta_usd'] += pos_delta_dollars
        ticker_risk[p['Underlying']]['theta_usd'] += pos_theta_dollars

    # 6. Agregación Final
    total_bwd = 0
    total_abs_exposure = 0
    for sym, data in ticker_risk.items():
        bwd = (data['delta_usd'] * data['beta']) / spy_price if spy_price > 0 else 0
        total_bwd += bwd
        total_abs_exposure += abs(data['delta_usd'])
    
    leverage = total_abs_exposure / net_liq if net_liq > 0 else 0

    return net_liq, total_raw_delta, total_bwd, total_theta, leverage, ticker_risk

# --- FLUJO DE UI ---

if TRADIER_TOKEN:
    if st.button("🔄 ACTUALIZAR DASHBOARD"):
        res = run_analysis()
        if res:
            nl, rd, bwd, th, lev, risk_map = res
            
            # Guardar en Historial
            new_entry = {
                "Timestamp": datetime.now().strftime("%H:%M:%S"),
                "Net_Liq": nl, "Delta_Neto": rd, "BWD_SPY": bwd, "Theta_Diario": th, "Apalancamiento": lev
            }
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([new_entry])], ignore_index=True)

            # KPIs
            st.markdown(f"### 🏦 Balance Neto: ${nl:,.2f}")
            col1, col2, col3, col4 = st.columns(4)
            
            colors = ["#4ade80" if x >= 0 else "#f87171" for x in [rd, bwd, th]]
            l_c = "#4ade80" if lev < 1.5 else "#facc15"
            
            col1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value" style="color:{colors[0]}">{rd:.1f}</div></div>', unsafe_allow_html=True)
            col2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value" style="color:{colors[1]}">{bwd:.1f}</div></div>', unsafe_allow_html=True)
            col3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:{colors[2]}">${th:.2f}</div></div>', unsafe_allow_html=True)
            col4.markdown(f'<div class="card" style="border-bottom:5px solid {l_c}"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value" style="color:{l_c}">{lev:.2f}x</div></div>', unsafe_allow_html=True)

            # --- SECCIÓN DE HISTORIAL ---
            st.divider()
            st.subheader("📈 Comportamiento en la Sesión")
            
            hist = st.session_state.history_df
            if len(hist) > 1:
                h_col1, h_col2 = st.columns(2)
                with h_col1:
                    st.caption("Evolución del BWD (Riesgo Direccional)")
                    st.line_chart(hist, x="Timestamp", y="BWD_SPY")
                with h_col2:
                    st.caption("Evolución del Theta (Renta Diaria)")
                    st.line_chart(hist, x="Timestamp", y="Theta_Diario")
                
                csv = hist.to_csv(index=False).encode('utf-8')
                st.download_button("💾 Guardar Datos del Día (CSV)", csv, f"log_trading_{datetime.now().strftime('%Y%m%d')}.csv")
            else:
                st.info("Presiona 'Actualizar' varias veces para ver la tendencia.")

            # Tablas de Riesgo
            st.divider()
            st.subheader("Desglose por Activo")
            st.table(pd.DataFrame([{"Activo": k, "Beta": f"{v['beta']:.2f}", "Delta $": f"{v['delta_usd']:,.0f}", "Theta $": f"{v['theta_usd']:.2f}"} for k,v in risk_map.items()]))
        else:
            st.error("No se pudo obtener información de la cuenta. Revisa tu token.")
else:
    st.info("👈 Ingresa tu Token para comenzar.")

