import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
import yfinance as yf
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master V15", page_icon="📈")

# Estilo Tom King
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center; height: 120px;
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 5px;}
    .roi-val {color: #2ea043; font-size: 1.5rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 8px 15px; 
        border-radius: 5px; margin: 25px 0 10px 0; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V15 - Direct Gain/Loss)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.info("Esta versión usa el reporte oficial de Ganancias Realizadas del broker.")

# --- FUNCIONES CORE ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    if not symbol or len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def run_v15_accounting():
    # 1. Identificar Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Posiciones Abiertas
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]

    # 3. GANANCIAS REALIZADAS (EL CAMBIO CLAVE V15)
    # Consultamos el endpoint de Gain/Loss que ya tiene los trades cerrados calculados por Tradier
    r_gl = requests.get(f"{BASE_URL}/accounts/{acct_id}/gainloss", headers=get_headers())
    gainloss_data = r_gl.json().get('gainloss', {}).get('closed_position', [])
    if isinstance(gainloss_data, dict): gainloss_data = [gainloss_data]

    # 4. Market Data Actual
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- PROCESAMIENTO ---
    report = {}

    # A. Identificar Leaps
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        # Leaps (Long y ITM)
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.50:
            if u_sym not in report:
                report[u_sym] = {"leaps": [], "realized": 0.0, "closed_list": [], "active_short": None, "spot": q_map.get(u_sym, {}).get('last', 0)}
            
            cost = abs(float(p.get('cost_basis', 0)))
            val = float(p['quantity']) * q_data.get('last', 0) * 100
            
            report[u_sym]['leaps'].append({
                "Date": p.get('date_acquired', 'N/A')[:10],
                "Exp": q_data.get('expiration_date'),
                "Strike": q_data.get('strike'),
                "Qty": p['quantity'],
                "Cost": cost,
                "Value": val,
                "P/L": val - cost
            })

    # B. Procesar Ganancias Realizadas
    for gl in gainloss_data:
        sym = gl.get('symbol', '')
        u_sym = get_underlying(sym)
        
        # Solo si es una opción de un activo donde tenemos PMCC
        if u_sym in report and len(sym) > 6:
            gain = float(gl.get('gain_loss', 0))
            report[u_sym]['realized'] += gain
            report[u_sym]['closed_list'].append({
                "Cerrado": gl.get('close_date', 'N/A')[:10],
                "Símbolo": sym[-8:],
                "P/L": gain,
                "DIT": gl.get('term', '-')
            })

    # C. Corto Activo (Monitor de Jugo)
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        if u_sym in report and float(p['quantity']) < 0:
            q = q_map.get(sym, {})
            u_p = report[u_sym]['spot']
            strike = q.get('strike', 0)
            opt_p = q.get('last', 0)
            juice = opt_p - max(0, u_p - strike)
            
            report[u_sym]['active_short'] = {
                "Strike": strike, "Price": opt_p, "Ext": juice,
                "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days
            }

    return report, gainloss_data

# --- INTERFAZ ---

if TOKEN:
    if st.button("🚀 ACTUALIZAR REPORTE MAESTRO"):
        data_tuple = run_v15_accounting()
        if data_tuple:
            report, raw_gl = data_tuple
            for ticker, d in report.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                t_cost = sum([l['Cost'] for l in d['leaps']])
                t_val = sum([l['Value'] for l in d['leaps']])
                realized = d['realized']
                net_inc = (t_val - t_cost) + realized
                roi = (net_inc / t_cost * 100) if t_cost > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${t_cost:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${t_val:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${realized:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${net_inc:,.2f}</p></div>', unsafe_allow_html=True)
                
                roi_col = "#4ade80" if roi > 0 else "#f87171"
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="metric-value" style="color:{roi_col}">{roi:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    a = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                    if a['Ext'] < 0.15: st.error("🚨 CRÍTICO: Poco valor extrínseco. Hora de ROLEAR.")

                if d['closed_list']:
                    with st.expander(f"📔 Ver Trades Cerrados ({ticker})"):
                        st.table(pd.DataFrame(d['closed_list']))
                
                st.divider()

            with st.expander("🔍 Auditoría de Ganancias (Raw Data)"):
                st.write("Si el CC Realizado sigue en cero, revisa si Tradier reporta aquí tus trades cerrados:")
                st.json(raw_gl)
        else:
            st.error("Error al conectar. Verifica el Token.")
else:
    st.info("👈 Introduce tu Token.")








