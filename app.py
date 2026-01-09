import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master V15.1", page_icon="📈")

# Estilo Tom King mejorado
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center; height: 120px;
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 5px;}
    .section-header {
        background-color: #238636; color: white; padding: 8px 15px; 
        border-radius: 5px; margin: 25px 0 10px 0; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V15.1 - Position Decoder)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"

# --- FUNCIONES DE DECODIFICACIÓN ---

def decode_occ_symbol(symbol):
    """
    Descompone un símbolo OCC (ej. SOFI251219C00010000)
    Retorna: (Underlying, Type, Strike)
    """
    if not symbol or len(symbol) < 15:
        return symbol, "STOCK", 0
    
    try:
        # El formato OCC es: ROOT (letras) + DATE (6 num) + TYPE (C/P) + STRIKE (8 num)
        match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
        if match:
            underlying = match.group(1)
            option_type = "CALL" if match.group(3) == "C" else "PUT"
            strike = float(match.group(4)) / 1000
            return underlying, option_type, strike
    except:
        pass
    return symbol, "UNKNOWN", 0

def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

# --- MOTOR DE DATOS ---

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

    # 3. Ganancias Realizadas
    r_gl = requests.get(f"{BASE_URL}/accounts/{acct_id}/gainloss", headers=get_headers())
    gainloss_data = r_gl.json().get('gainloss', {}).get('closed_position', [])
    if isinstance(gainloss_data, dict): gainloss_data = [gainloss_data]

    # 4. Market Data
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    report = {}

    # A. Identificar Leaps
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.50:
            if u_sym not in report:
                report[u_sym] = {"leaps": [], "realized_cc": 0.0, "closed_list": [], "active_short": None, "spot": q_map.get(u_sym, {}).get('last', 0)}
            
            cost = abs(float(p.get('cost_basis', 0)))
            val = float(p['quantity']) * q_data.get('last', 0) * 100
            
            report[u_sym]['leaps'].append({
                "Date": p.get('date_acquired', 'N/A')[:10],
                "Exp": q_data.get('expiration_date'),
                "Strike": q_data.get('strike'),
                "Qty": p['quantity'],
                "Cost": cost,
                "MarketVal": val,
                "P/L": val - cost
            })

    # B. Procesar Ganancias Realizadas decodificando el Símbolo
    for gl in gainloss_data:
        sym = gl.get('symbol', '')
        u_sym, opt_type, strike = decode_occ_symbol(sym)
        
        if u_sym in report and opt_type != "STOCK":
            gain = float(gl.get('gain_loss', 0))
            
            # Clasificación: Si el strike es alto, asumimos que es el CC (Income)
            # Si el strike es muy bajo (ITM), podría ser el LEAPS. 
            # Aquí lo mostramos todo pero etiquetado.
            
            report[u_sym]['realized_cc'] += gain
            report[u_sym]['closed_list'].append({
                "Cerrado": gl.get('close_date', 'N/A')[:10],
                "Tipo": opt_type,
                "Strike": strike,
                "P/L": gain,
                "DIT": gl.get('term', '-')
            })

    # C. Corto Activo
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

    return report

# --- INTERFAZ ---

if TOKEN:
    if st.button("🚀 ACTUALIZAR REPORTE MAESTRO"):
        data = run_v15_accounting()
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                t_cost = sum([l['Cost'] for l in d['leaps']])
                t_val = sum([l['MarketVal'] for l in d['leaps']])
                realized = d['realized_cc']
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
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "MarketVal": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    a = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")

                if d['closed_list']:
                    with st.expander(f"📔 Ver Historial Detallado de Trades Cerrados ({ticker})"):
                        # Convertir a DataFrame y dar formato
                        df_closed = pd.DataFrame(d['closed_list'])
                        st.dataframe(df_closed.style.format({"P/L": "${:,.2f}", "Strike": "{:.2f}"}), use_container_width=True)
                
                st.divider()
        else:
            st.error("Error al obtener datos o no hay campañas PMCC detectadas.")
else:
    st.info("👈 Introduce tu Token.")
