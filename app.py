import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master Accountant V12", page_icon="🧾")

# Estilo CSS Tom King 
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center; height: 120px;
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 5px;}
    .roi-pos {color: #4ade80; font-size: 1.4rem; font-weight: bold;}
    .roi-neg {color: #f87171; font-size: 1.4rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 8px 15px; 
        border-radius: 5px; margin: 25px 0 10px 0; font-size: 1.1rem; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V12 - Cash Flow)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.info("Auditoría de flujo de caja para opciones cerradas.")

# --- FUNCIONES CORE ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    if not symbol or len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def run_v12_accounting():
    # 1. Obtener Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Posiciones Abiertas (El "Ahora")
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]
    
    current_option_symbols = [p['symbol'] for p in positions if len(p['symbol']) > 6]

    # 3. Historial Extendido (Últimos 1000 eventos)
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", params={'limit': 1000}, headers=get_headers())
    history = r_hist.json().get('history', {}).get('event', []) if r_hist.status_code == 200 else []
    if isinstance(history, dict): history = [history]

    # 4. Precios actuales
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- PROCESAMIENTO ---
    report = {}

    # Identificar Activos con LEAPS
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.50:
            if u_sym not in report:
                report[u_sym] = {"leaps": [], "realized": 0, "audit_log": [], "active_short": None, "spot": q_map.get(u_sym, {}).get('last', 0)}
            
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

    # --- MOTOR FORENSE DE FLUJO DE CAJA ---
    for h in history:
        if h.get('type') == 'trade' and 'symbol' in h:
            sym = h['symbol']
            u_sym = get_underlying(sym)
            
            # Solo procesar si el activo es un PMCC identificado y es una opción
            if u_sym in report and len(sym) > 6:
                # Si la opción del historial YA NO está en posiciones abiertas
                if sym not in current_option_symbols:
                    price = float(h.get('price', 0))
                    qty = abs(float(h.get('quantity', 0)))
                    side = h.get('side', '').lower()
                    
                    # Lógica de Caja: Vender recibes (+), Comprar pagas (-)
                    cash_flow = price * qty * 100
                    if 'buy' in side: cash_flow = -cash_flow
                    
                    report[u_sym]['realized'] += cash_flow
                    report[u_sym]['audit_log'].append({
                        "Fecha": h['date'][:10],
                        "Acción": side.upper(),
                        "Contrato": sym[-8:],
                        "Monto": cash_flow
                    })

    # Identificar Corto Activo
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        if u_sym in report and float(p['quantity']) < 0:
            q = q_map.get(sym, {})
            strike = q.get('strike', 0)
            opt_p = q.get('last', 0)
            juice = opt_p - max(0, report[u_sym]['spot'] - strike)
            
            report[u_sym]['active_short'] = {
                "Strike": strike, "Price": opt_p, "Ext": juice,
                "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days
            }

    return report

# --- INTERFAZ ---
if TOKEN:
    if st.button("🚀 ACTUALIZAR AUDITORÍA DE CAJA"):
        data = run_v12_accounting()
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                total_cost = sum([l['Cost'] for l in d['leaps']])
                total_val = sum([l['MarketVal'] for l in d['leaps']])
                realized = d['realized']
                net_inc = (total_val - total_cost) + realized
                roi = (net_inc / total_cost * 100) if total_cost > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${total_cost:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${total_val:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${realized:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${net_inc:,.2f}</p></div>', unsafe_allow_html=True)
                
                roi_col = "#4ade80" if roi > 0 else "#f87171"
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="metric-value" style="color:{roi_col}">{roi:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    a = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                    if a['Ext'] < 0.15: st.error("🚨 TIEMPO DE ROLEAR")

                if d['audit_log']:
                    with st.expander(f"📔 Ver Auditoría de Flujo de Caja ({ticker})"):
                        st.table(pd.DataFrame(d['audit_log']).sort_values("Fecha", ascending=False))
                st.divider()
        else:
            st.warning("No hay campañas activas detectadas.")
else:
    st.info("👈 Introduce tu Token en la barra lateral.")





