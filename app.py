import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime, timedelta

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Forensic V13", page_icon="🧾")

# Estilo CSS Tom King Style
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center; height: 120px;
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 5px;}
    .roi-pos {color: #2ea043; font-size: 1.4rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 8px 15px; 
        border-radius: 5px; margin: 25px 0 10px 0; font-size: 1.1rem; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V13 - Deep History)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.info("Buscando historial desde 2025-01-01 para capturar todas las primas.")

# --- FUNCIONES CORE ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    if not symbol or len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def run_v13_accounting():
    # 1. Obtener Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Posiciones Abiertas
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]
    
    current_symbols = [p['symbol'] for p in positions]

    # 3. HISTORIAL DE UN AÑO (AQUÍ ESTÁ EL CAMBIO CLAVE)
    # Pedimos desde el 1 de Enero de 2025 para asegurar que entre todo tu Excel
    start_date = "2025-01-01"
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", 
                          params={'limit': 1000, 'start': start_date}, 
                          headers=get_headers())
    history = r_hist.json().get('history', {}).get('event', []) if r_hist.status_code == 200 else []
    if isinstance(history, dict): history = [history]

    # 4. Precios actuales
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- PROCESAMIENTO ---
    report = {}
    full_audit_log = []

    # Identificar Activos con LEAPS
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        # Leaps (Long y Delta alto)
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.50:
            if u_sym not in report:
                report[u_sym] = {"leaps": [], "realized": 0, "history_details": [], "active_short": None, "spot": q_map.get(u_sym, {}).get('last', 0)}
            
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

    # --- MOTOR DE AUDITORÍA DE CAJA ---
    for h in history:
        if h.get('type') == 'trade' and 'symbol' in h:
            sym = h['symbol']
            u_sym = get_underlying(sym)
            
            # Solo procesamos si el activo tiene un LEAPS abierto y es una opción (símbolo largo)
            if u_sym in report and len(sym) > 6:
                side = h.get('side', '').lower()
                price = float(h.get('price', 0))
                qty = abs(float(h.get('quantity', 0)))
                
                # Dinero que entra (+) o sale (-)
                cash_impact = price * qty * 100
                if 'buy' in side: cash_impact = -cash_impact
                
                # Si la opción ya no está abierta, es 100% Realizado
                # Si es el Short Call actual, no lo sumamos al realizado todavía (es flotante)
                if sym not in current_symbols:
                    report[u_sym]['realized'] += cash_impact
                    report[u_sym]['history_details'].append({
                        "Fecha": h['date'][:10],
                        "Acción": side.upper(),
                        "Strike": sym[-8:],
                        "Monto": cash_impact
                    })
                
                full_audit_log.append(h) # Para el debug de abajo

    # Identificar Corto Activo
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

    return report, full_audit_log

# --- INTERFAZ ---
if TOKEN:
    if st.button("🚀 ACTUALIZAR AUDITORÍA PROFUNDA"):
        res = run_v13_accounting()
        if res:
            report, raw_log = res
            for ticker, d in report.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker}</div>', unsafe_allow_html=True)
                
                t_cost = sum([l['Cost'] for l in d['leaps']])
                t_val = sum([l['MarketVal'] for l in d['leaps']])
                realized = d['realized']
                net_inc = (t_val - t_cost) + realized
                roi = (net_inc / t_cost * 100) if t_cost > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${t_cost:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${t_val:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${realized:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${net_inc:,.2f}</p></div>', unsafe_allow_html=True)
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="roi-pos">{roi:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    a = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                
                if d['history_details']:
                    with st.expander(f"📔 Historial de Primas Recolectadas ({ticker})"):
                        st.table(pd.DataFrame(d['history_details']))
                st.divider()

            with st.expander("🔍 DEBUG: Ver historial crudo de la API"):
                st.write("Esta es la lista de eventos que Tradier reportó. Si no ves tus trades de hace meses aquí, aumenta el rango de fecha.")
                st.json(raw_log)
        else:
            st.error("Error al conectar con Tradier.")
else:
    st.info("👈 Introduce tu Token.")






