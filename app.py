import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import re
import time

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="Income Trader Risk Dashboard", page_icon="🛡️")

# Estilos CSS Profesionales para Tema Oscuro
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

st.title("🛡️ Income Trader: Risk & Greeks Manager")

# --- SIDEBAR: CONEXIÓN ---
with st.sidebar:
    st.header("📡 Conexión Broker")
    TRADIER_TOKEN = st.text_input("Tradier Access Token", type="password", placeholder="Ingresa tu token...")
    env_mode = st.radio("Entorno", ["Producción (Real)", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción (Real)" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.caption("v8.2.0 | Dashboard Stable")

# --- FUNCIONES AUXILIARES ---
def map_to_yahoo(symbol):
    s = symbol.upper().strip()
    if s in ['SPX', 'SPXW', 'SPX.X']: return '^SPX'
    if s in ['NDX', 'NDXW', 'NDX.X']: return '^NDX'
    if s in ['RUT', 'RUTW', 'RUT.X']: return '^RUT'
    if s in ['VIX', 'VIX.X']: return '^VIX'
    if '/' in s: return s.replace('/', '-') 
    return s

def get_underlying_symbol(symbol):
    if len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def get_headers():
    return {"Authorization": f"Bearer {TRADIER_TOKEN}", "Accept": "application/json"}

# --- OBTENCIÓN DE DATOS ---
def get_account_balance():
    try:
        r = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
        if r.status_code != 200: return None, 0
        acct = r.json()['profile']['account']
        acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']
        r_bal = requests.get(f"{BASE_URL}/accounts/{acct_id}/balances", headers=get_headers())
        return acct_id, float(r_bal.json()['balances']['total_equity'])
    except:
        return None, 0

def get_portfolio_data(acct_id):
    try:
        r = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
        data = r.json()
        if 'positions' not in data or data['positions'] == 'null' or data['positions'] is None: return [], 0
        
        raw_pos = data['positions']['position']
        if isinstance(raw_pos, dict): raw_pos = [raw_pos]
        
        positions = []
        symbols_to_fetch = []
        for p in raw_pos:
            sym = p['symbol']
            qty = float(p['quantity'])
            u_sym = get_underlying_symbol(sym)
            type_ = "Option" if len(sym) > 5 else "Stock"
            positions.append({
                "Symbol": sym, "Type": type_, "Qty": qty, 
                "Underlying": u_sym, "Delta": 0.0, "Theta": 0.0, "Underlying_Price": 0.0
            })
            symbols_to_fetch.extend([sym, u_sym])
        
        unique_symbols = list(set(symbols_to_fetch + ["SPY"]))
        market_data = {}
        chunk_size = 50
        for i in range(0, len(unique_symbols), chunk_size):
            chunk = ",".join(unique_symbols[i:i+chunk_size])
            r_qs = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': chunk, 'greeks': 'true'}, headers=get_headers())
            if r_qs.status_code == 200:
                quotes = r_qs.json().get('quotes', {}).get('quote', [])
                if isinstance(quotes, dict): quotes = [quotes]
                for q in quotes:
                    delta = q.get('greeks', {}).get('delta', 1.0 if len(q['symbol'])<6 else 0.0)
                    theta = q.get('greeks', {}).get('theta', 0.0)
                    market_data[q['symbol']] = {
                        'price': float(q.get('last', 0) or 0),
                        'delta': float(delta or 0),
                        'theta': float(theta or 0)
                    }
        for pos in positions:
            m_data = market_data.get(pos['Symbol'])
            u_data = market_data.get(pos['Underlying'])
            if m_data:
                pos['Delta'] = m_data['delta']
                pos['Theta'] = m_data['theta']
            if u_data:
                pos['Underlying_Price'] = u_data['price']
        return positions, market_data.get('SPY', {}).get('price', 0)
    except:
        return [], 0

# --- BETA ENGINE ---
def clean_data(df):
    if isinstance(df, pd.Series): df = df.to_frame()
    if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
    col = 'Adj Close' if 'Adj Close' in df.columns else ('Close' if 'Close' in df.columns else df.columns[0])
    df = df[[col]].copy()
    df.index = df.index.tz_localize(None)
    return df

@st.cache_data(ttl=3600)
def calculate_beta_individual(ticker, spy_returns):
    if ticker in ['BIL', 'SGOV', 'SHV']: return 0.0
    yahoo_sym = map_to_yahoo(ticker)
    try:
        stock_raw = yf.download(yahoo_sym, period="1y", progress=False)
        if stock_raw.empty: return 1.0
        stock_returns = clean_data(stock_raw).pct_change().dropna()
        aligned = pd.concat([stock_returns, spy_returns], axis=1, join='inner').dropna()
        if len(aligned) < 10: return 1.0
        return aligned.iloc[:,0].cov(aligned.iloc[:,1]) / aligned.iloc[:,1].var()
    except:
        return 1.0

# --- LÓGICA PRINCIPAL ---
if TRADIER_TOKEN:
    if st.button("🔄 ACTUALIZAR DASHBOARD"):
        with st.spinner("Analizando riesgo institucional..."):
            acct_id, net_liq = get_account_balance()
            if acct_id:
                positions, spy_price = get_portfolio_data(acct_id)
                if positions:
                    spy_raw = yf.download("SPY", period="1y", progress=False)
                    spy_returns = clean_data(spy_raw).pct_change().dropna()
                    underlyings = list(set([p['Underlying'] for p in positions]))
                    betas = {t: calculate_beta_individual(t, spy_returns) for t in underlyings}
                    
                    ticker_risk = {}
                    detailed_rows = []
                    total_portfolio_raw_delta = 0
                    total_portfolio_theta = 0
                    
                    for p in positions:
                        u_sym = p['Underlying']
                        u_price = p['Underlying_Price']
                        mult = 100 if p['Type'] == 'Option' else 1
                        pos_delta_dollars = p['Qty'] * p['Delta'] * mult * u_price
                        pos_raw_delta = p['Qty'] * p['Delta'] * mult
                        pos_theta_dollars = p['Qty'] * p['Theta'] * mult
                        
                        total_portfolio_raw_delta += pos_raw_delta
                        total_portfolio_theta += pos_theta_dollars
                        
                        if u_sym not in ticker_risk:
                            ticker_risk[u_sym] = {'net_delta_dollars': 0.0, 'net_theta': 0.0, 'net_raw_delta': 0.0, 'price': u_price, 'beta': betas.get(u_sym, 1.0)}
                        
                        ticker_risk[u_sym]['net_delta_dollars'] += pos_delta_dollars
                        ticker_risk[u_sym]['net_theta'] += pos_theta_dollars
                        ticker_risk[u_sym]['net_raw_delta'] += pos_raw_delta
                        detailed_rows.append(p)

                    grouped_data = []
                    total_bwd = 0
                    total_net_exposure_abs = 0
                    for sym, data in ticker_risk.items():
                        bwd = (data['net_delta_dollars'] * data['beta']) / spy_price if spy_price > 0 else 0
                        total_bwd += bwd
                        total_net_exposure_abs += abs(data['net_delta_dollars'])
                        grouped_data.append({
                            "Activo": sym, "Precio": data['price'], "Beta": data['beta'],
                            "Delta Puro": data['net_raw_delta'], "Net Delta $": data['net_delta_dollars'],
                            "Net Theta $": data['net_theta'], "BWD": bwd
                        })
                    
                    leverage = total_net_exposure_abs / net_liq if net_liq > 0 else 0

                    st.markdown(f"### 🏦 Balance Neto: ${net_liq:,.2f}")
                    col1, col2, col3, col4 = st.columns(4)
                    d_c = "#4ade80" if total_portfolio_raw_delta > 0 else "#f87171"
                    col1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value" style="color:{d_c}">{total_portfolio_raw_delta:.2f}</div><div class="metric-sub">Suma Deltas Puros</div></div>', unsafe_allow_html=True)
                    b_c = "#4ade80" if total_bwd > 0 else "#f87171"
                    col2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value" style="color:{b_c}">{total_bwd:.2f}</div><div class="metric-sub">Riesgo vs SPY</div></div>', unsafe_allow_html=True)
                    t_c = "#4ade80" if total_portfolio_theta > 0 else "#f87171"
                    col3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:{t_c}">${total_portfolio_theta:.2f}</div><div class="metric-sub">Ingreso Diario</div></div>', unsafe_allow_html=True)
                    l_c = "#4ade80" if leverage < 1.5 else ("#facc15" if leverage < 2.5 else "#f87171")
                    col4.markdown(f'<div class="card" style="border-bottom: 5px solid {l_c}"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value" style="color:{l_c}">{leverage:.2f}x</div><div class="metric-sub">Nocional / Cash</div></div>', unsafe_allow_html=True)

                    st.divider()
                    st.subheader("Riesgo Neto por Activo")
                    df_g = pd.DataFrame(grouped_data).sort_values(by='BWD', key=abs, ascending=False)
                    st.dataframe(df_g.style.format({"Precio": "${:.2f}", "Beta": "{:.2f}", "Delta Puro": "{:.1f}", "Net Delta $": "${:,.0f}", "Net Theta $": "${:,.2f}", "BWD": "{:.2f}"}), use_container_width=True)

                    with st.expander("Ver Detalle de Contratos"):
                        st.dataframe(pd.DataFrame(detailed_rows).style.format({"Underlying_Price": "${:.2f}", "Delta": "{:.4f}", "Theta": "{:.4f}"}), use_container_width=True)
                else:
                    st.warning("No hay posiciones abiertas.")
            else:
                st.error("Error de conexión. Revisa tu Token.")
else:
    st.info("👈 Ingresa tu **Tradier Access Token** en la barra lateral para sincronizar tu cartera.")
