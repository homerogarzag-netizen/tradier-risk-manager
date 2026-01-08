import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Forensic Accountant", page_icon="🧾")

# Estilo CSS para imitar la hoja de Tom King
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center; height: 110px;
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 5px;}
    .roi-pos {color: #2ea043; font-size: 1.5rem; font-weight: bold;}
    .roi-neg {color: #f87171; font-size: 1.5rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 8px 15px; 
        border-radius: 5px; margin: 25px 0 10px 0; font-size: 1.1rem; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V11.9 - Forensic)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"

# --- FUNCIONES CORE ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    if not symbol or len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def run_forensic_accounting():
    # 1. Identificar Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Posiciones Actuales (El "Ahora")
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]

    # 3. Historial Completo (El "Pasado") - 1000 registros
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", params={'limit': 1000}, headers=get_headers())
    history = r_hist.json().get('history', {}).get('event', []) if r_hist.status_code == 200 else []
    if isinstance(history, dict): history = [history]

    # 4. Precios en Vivo
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- LÓGICA DE AUDITORÍA ---
    report = {}
    current_symbols = [p['symbol'] for p in positions]

    # A. Identificar Leaps y crear contenedores por Activo
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        # Criterio LEAPS (Long e ITM)
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.55:
            if u_sym not in report:
                report[u_sym] = {"leaps": [], "realized_cash": 0, "closed_trades": [], "active_short": None, "spot": q_map.get(u_sym, {}).get('last', 0)}
            
            cost = abs(float(p.get('cost_basis', 0)))
            val = float(p['quantity']) * q_data.get('last', 0) * 100
            
            report[u_sym]['leaps'].append({
                "Adquirido": p.get('date_acquired', 'N/A')[:10],
                "Exp": q_data.get('expiration_date'),
                "Strike": q_data.get('strike'),
                "Qty": p['quantity'],
                "Cost": cost,
                "Value": val,
                "P/L": val - cost
            })

    # B. Auditoría de Historial (Forensic Pairing)
    # Buscamos en el historial todos los trades de opciones que ya NO están en posiciones actuales
    if history:
        for h in history:
            if h.get('type') == 'trade' and 'symbol' in h:
                sym = h['symbol']
                u_sym = get_underlying(sym)
                
                # Solo procesamos si el activo es uno de nuestros PMCC
                if u_sym in report and len(sym) > 6:
                    # Calculamos el flujo de caja: Vender es +, Comprar es -
                    price = float(h.get('price', 0))
                    qty = float(h.get('quantity', 0))
                    side = h.get('side', '')
                    
                    # El P/L de una opción corta cerrada o expirada:
                    # Si vendimos (STO): +dinero
                    # Si compramos (BTC): -dinero
                    # Tradier usa side: 'sell_to_open', 'buy_to_close', etc.
                    
                    amount = 0
                    if 'sell' in side: amount = abs(price) * 100 * abs(qty)
                    if 'buy' in side: amount = -abs(price) * 100 * abs(qty)
                    
                    # Si la opción ya no está abierta, este dinero es 100% Realizado
                    if sym not in current_symbols:
                        report[u_sym]['realized_cash'] += amount
                        report[u_sym]['closed_trades'].append({
                            "Fecha": h['date'][:10],
                            "Tipo": side.upper(),
                            "Contrato": sym[-8:],
                            "Monto": amount
                        })

    # C. Identificar Short Activo (Monitor de Jugo)
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
    if st.button("🚀 ACTUALIZAR REPORTE FORENSE"):
        data = run_forensic_accounting()
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                # Totales
                total_leaps_cost = sum([l['Cost'] for l in d['leaps']])
                total_leaps_val = sum([l['Value'] for l in d['leaps']])
                realized = d['realized_cash']
                
                # El Net Income es (Ganancia/Pérdida LEAPS) + (Ingresos por cortos cerrados)
                net_income = (total_leaps_val - total_leaps_cost) + realized
                roi = (net_income / total_leaps_cost * 100) if total_leaps_cost > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${total_leaps_cost:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${total_leaps_val:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${realized:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${net_income:,.2f}</p></div>', unsafe_allow_html=True)
                
                r_style = "roi-pos" if roi > 0 else "roi-neg"
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="{r_style}">{roi:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    a = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                    if a['Ext'] < 0.15: st.error("🚨 TIEMPO DE ROLEAR")

                if d['closed_trades']:
                    with st.expander(f"📔 Ver Auditoría de Movimientos ({ticker})"):
                        st.table(pd.DataFrame(d['closed_trades']).sort_values("Fecha", ascending=False))
                st.divider()
        else:
            st.warning("No se detectaron campañas PMCC activas.")
else:
    st.info("👈 Introduce tu Token.")




