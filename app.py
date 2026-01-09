import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master V15.3", page_icon="📈")

# Estilo visual Tom King
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center; height: 125px;
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 5px;}
    .roi-val {color: #2ea043; font-size: 1.5rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 8px 15px; 
        border-radius: 5px; margin: 25px 0 10px 0; font-weight: bold;
    }
    .income-tag {color: #4ade80; font-weight: bold;}
    .core-tag {color: #00d4ff; font-weight: bold;}
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V15.3 - Forensic Filter)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"

# --- FUNCIONES DE DECODIFICACIÓN ---

def decode_occ_symbol(symbol):
    """Extrae datos del símbolo OCC (ej. SOFI251219C00010000)"""
    if not symbol or len(symbol) < 15:
        return symbol, "STOCK", 0
    try:
        match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", symbol)
        if match:
            option_type = "CALL" if match.group(3) == "C" else "PUT"
            strike = float(match.group(4)) / 1000
            return match.group(1), option_type, strike
    except: pass
    return symbol, "UNKNOWN", 0

def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

# --- MOTOR DE DATOS ---

def run_v15_3_analysis():
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

    # 3. Ganancias Realizadas (Historial de Trades Cerrados)
    r_gl = requests.get(f"{BASE_URL}/accounts/{acct_id}/gainloss", headers=get_headers())
    gl_data = r_gl.json().get('gainloss', {}).get('closed_position', [])
    if isinstance(gl_data, dict): gl_data = [gl_data]

    # 4. Market Data Actual
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    report = {}

    # A. Identificar Leaps (CORE)
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.50:
            if u_sym not in report:
                report[u_sym] = {
                    "leaps": [], 
                    "leaps_strikes": [],
                    "realized_cc": 0.0, 
                    "closed_list": [], 
                    "active_short": None, 
                    "spot": q_map.get(u_sym, {}).get('last', 0)
                }
            
            c = abs(float(p.get('cost_basis', 0)))
            v = float(p['quantity']) * q_data.get('last', 0) * 100
            s_val = q_data.get('strike', 0)
            
            report[u_sym]['leaps_strikes'].append(s_val)
            report[u_sym]['leaps'].append({
                "Adquirido": p.get('date_acquired', 'N/A')[:10],
                "Exp": q_data.get('expiration_date'),
                "Strike": s_val,
                "Qty": p['quantity'],
                "Cost": c, "Value": v, "P/L": v - c
            })

    # B. Auditoría Filtrada de Trades Cerrados
    for gl in gl_data:
        sym = gl.get('symbol', '')
        u_sym, opt_type, strike = decode_occ_symbol(sym)
        
        if u_sym in report and opt_type == "CALL":
            gain = float(gl.get('gain_loss', 0))
            
            # Clasificación Lógica:
            # Si el strike cerrado es uno de nuestros LEAPS strikes -> Es Core
            # Si es mayor -> Es Renta (Covered Call)
            is_core = any(abs(strike - ls) < 0.5 for ls in report[u_sym]['leaps_strikes'])
            
            category = "CORE (Leaps)" if is_core else "INCOME (CC)"
            # Para el Income de Tom King, la acción siempre es STO -> BTC
            action = "BTO → STC" if is_core else "STO → BTC"
            
            # Solo sumamos al CC Realizado si es INCOME
            if not is_core:
                report[u_sym]['realized_cc'] += gain
            
            report[u_sym]['closed_list'].append({
                "Categoría": category,
                "Flujo": action,
                "Abierto": gl.get('open_date', 'N/A')[:10],
                "Cerrado": gl.get('close_date', 'N/A')[:10],
                "Strike": strike,
                "P/L": gain,
                "DIT": gl.get('term', '-')
            })

    # C. Corto Activo
    for p in positions:
        u_sym = get_underlying(p['symbol'])
        if u_sym in report and float(p['quantity']) < 0:
            q = q_map.get(p['symbol'], {})
            u_p = report[u_sym]['spot']
            strike = q.get('strike', 0)
            opt_p = q.get('last', 0)
            report[u_sym]['active_short'] = {
                "Strike": strike, "Price": opt_p, "Ext": opt_p - max(0, u_p - strike),
                "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days
            }

    return report

# --- INTERFAZ ---

if TOKEN:
    if st.button("🚀 GENERAR AUDITORÍA CONTABLE"):
        data = run_v15_3_analysis()
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                # KPIs
                tc = sum([l['Cost'] for l in d['leaps']])
                tv = sum([l['Value'] for l in d['leaps']])
                re = d['realized_cc']
                ni = (tv - tc) + re
                ro = (ni / tc * 100) if tc > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${tc:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${tv:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO (INCOME)</p><p class="kpi-value" style="color:#4ade80">${re:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${ni:,.2f}</p></div>', unsafe_allow_html=True)
                
                r_c = "#4ade80" if ro > 0 else "#f87171"
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="kpi-value" style="color:{r_c}">{ro:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    ash = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {ash['Strike']} | DTE: {ash['DTE']} | **Extrínseco: ${ash['Ext']:.2f}**")

                if d['closed_list']:
                    with st.expander(f"📔 Ver Historial Detallado de Trades Cerrados ({ticker})"):
                        df_cl = pd.DataFrame(d['closed_list']).sort_values("Cerrado", ascending=False)
                        
                        # Colorear Categoría
                        def color_cat(val):
                            color = '#4ade80' if 'INCOME' in val else '#00d4ff'
                            return f'color: {color}; font-weight: bold'
                            
                        st.dataframe(df_cl.style.applymap(color_cat, subset=['Categoría'])
                                     .format({"P/L": "${:,.2f}", "Strike": "{:.2f}"}), 
                                     use_container_width=True)
                
                st.divider()
        else:
            st.error("No se detectaron campañas PMCC activas.")
else:
    st.info("👈 Introduce tu Token.")


