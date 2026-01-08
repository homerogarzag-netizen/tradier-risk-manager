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
    st.caption("v9.3.0 | Full View Stable")

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
    if raw is None or raw == 'null': return net_liq, 0, 0, 0, 0, {}, []
    if isinstance(raw, dict): raw = [raw]

    # Market Data
    syms = ["SPY"] + [p['symbol'] for p in raw] + [get_underlying_symbol(p['symbol']) for p in raw]
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(list(set(syms))), 'greeks': 'true'}, headers=get_headers())
    m_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])}
    spy_p = float(m_map.get('SPY', {}).get('last', 685))

    # Yahoo Data para Betas
    spy_df = yf.download("SPY", period="1y", progress=False)
    spy_ret = (spy_df['Adj Close'] if 'Adj Close' in spy_df.columns else spy_df['Close']).tz_localize(None).pct_change().dropna()
    
    # Greeks & Netting
    total_raw_delta, total_theta, total_abs_exp, total_bwd = 0, 0, 0, 0
    t_map = {}
    detailed_list = []
    
    for p in raw:
        qty = float(p['quantity'])
        u_sym = get_underlying_symbol(p['symbol'])
        m_d = m_map.get(p['symbol'], {})
        u_p = float(m_map.get(u_sym, {}).get('last', 0))
        mult = 100 if len(p['symbol']) > 5 else 1
        
        d = float(m_d.get('greeks', {}).get('delta', 1.0 if mult==1 else 0))
        th = float(m_d.get('greeks', {}).get('theta', 0))
        
        if u_sym not in t_map:
            t_map[u_sym] = {'delta_usd': 0, 'theta_usd': 0, 'delta_puro': 0, 'beta': calculate_beta(u_sym, spy_ret), 'price': u_p}
        
        t_map[u_sym]['delta_usd'] += (qty * d * mult * u_p)
        t_map[u_sym]['theta_usd'] += (qty * th * mult)
        t_map[u_sym]['delta_puro'] += (qty * d * mult)
        
        total_raw_delta += (qty * d * mult)
        total_theta += (qty * th * mult)
        
        detailed_list.append({
            "Símbolo": p['symbol'], "Tipo": "Opción" if mult==100 else "Acción", 
            "Qty": qty, "Underlying": u_sym, "Delta": d, "Theta": th, "Price": u_p
        })

    for s, data in t_map.items():
        total_bwd += (data['delta_usd'] * data['beta']) / spy_p if spy_p > 0 else 0
        total_abs_exp += abs(data['delta_usd'])

    return net_liq, total_raw_delta, total_bwd, total_theta, total_abs_exp/net_liq, t_map, detailed_list

# --- FLUJO DE UI ---

if TRADIER_TOKEN:
    if st.button("🔄 ACTUALIZAR DASHBOARD"):
        res = run_analysis()
        if res:
            nl, rd, bwd, th, lev, r_map, d_list = res
            
            # Actualizar Historial
            new_entry = {
                "Timestamp": datetime.now().strftime("%H:%M:%S"),
                "Net_Liq": nl, "Delta_Neto": rd, "BWD_SPY": bwd, "Theta_Diario": th, "Apalancamiento": lev
            }
            st.session_state.history_df = pd.concat([st.session_state.history_df, pd.DataFrame([new_entry])], ignore_index=True)

            # --- HEADER ---
            st.markdown(f"### 🏦 Balance Neto: ${nl:,.2f}")
            c1, c2, c3, c4 = st.columns(4)
            
            colors = ["#4ade80" if x >= 0 else "#f87171" for x in [rd, bwd, th]]
            l_c = "#4ade80" if lev < 1.5 else ("#facc15" if lev < 2.2 else "#f87171")
            
            c1.markdown(f'<div class="card"><div class="metric-label">DELTA NETO</div><div class="metric-value" style="color:{colors[0]}">{rd:.1f}</div><div class="metric-sub">Deltas Puros</div></div>', unsafe_allow_html=True)
            c2.markdown(f'<div class="card"><div class="metric-label">BWD (SPY)</div><div class="metric-value" style="color:{colors[1]}">{bwd:.1f}</div><div class="metric-sub">Riesgo Beta-Ajustado</div></div>', unsafe_allow_html=True)
            c3.markdown(f'<div class="card"><div class="metric-label">THETA DIARIO</div><div class="metric-value" style="color:{colors[2]}">${th:.2f}</div><div class="metric-sub">Paso del Tiempo</div></div>', unsafe_allow_html=True)
            c4.markdown(f'<div class="card" style="border-bottom:5px solid {l_c}"><div class="metric-label">APALANCAMIENTO</div><div class="metric-value" style="color:{l_c}">{lev:.2f}x</div><div class="metric-sub">Nocional / Cash</div></div>', unsafe_allow_html=True)

            # --- HISTORIAL ---
            st.divider()
            st.subheader("📈 Comportamiento de la Sesión")
            h = st.session_state.history_df
            if len(h) > 1:
                row1_c1, row1_c2 = st.columns(2)
                with row1_c1:
                    st.caption("💰 Evolución Balance Neto")
                    st.area_chart(h, x="Timestamp", y="Net_Liq", color="#00d4ff")
                with row1_c2:
                    st.caption("⚖️ Evolución BWD (Riesgo SPY)")
                    st.line_chart(h, x="Timestamp", y="BWD_SPY", color="#4ade80")

                row2_c1, row2_c2, row2_c3 = st.columns(3)
                with row2_c1: st.line_chart(h, x="Timestamp", y="Delta_Neto")
                with row2_c2: st.line_chart(h, x="Timestamp", y="Theta_Diario")
                with row2_c3: st.line_chart(h, x="Timestamp", y="Apalancamiento")
                
                csv = h.to_csv(index=False).encode('utf-8')
                st.download_button("💾 Exportar Sesión (.csv)", csv, f"trading_log_{datetime.now().strftime('%H%M')}.csv")

            # --- TABLA 1: RIESGO NETO POR ACTIVO ---
            st.divider()
            st.subheader("📊 Riesgo Neto por Activo")
            
            # Preparamos los datos del mapa para la tabla sin errores de nombres
            grouped_rows = []
            for k, v in r_map.items():
                grouped_rows.append({
                    "Activo": k,
                    "Precio": v['price'],
                    "Beta": v['beta'],
                    "Delta Puro": v['delta_puro'],
                    "Net Delta $": v['delta_usd'],
                    "Net Theta $": v['theta_usd'],
                    "BWD": (v['delta_usd'] * v['beta']) / spy_price if spy_price > 0 else 0
                })
            
            df_grouped = pd.DataFrame(grouped_rows).sort_values(by='BWD', key=abs, ascending=False)
            st.dataframe(df_grouped.style.format({
                "Precio": "${:.2f}", "Beta": "{:.2f}", "Delta Puro": "{:.1f}",
                "Net Delta $": "${:,.0f}", "Net Theta $": "${:,.2f}", "BWD": "{:.2f}"
            }), use_container_width=True)

            # --- TABLA 2: DETALLE INDIVIDUAL ---
            with st.expander("🔍 Ver Detalle de Contratos Individuales"):
                st.dataframe(pd.DataFrame(d_list).style.format({
                    "Delta": "{:.4f}", "Theta": "{:.4f}", "Price": "${:.2f}"
                }), use_container_width=True)
else:
    st.info("👈 Ingresa tu Token para iniciar.")
