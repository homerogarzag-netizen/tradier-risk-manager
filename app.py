import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master Accountant", page_icon="🧾")

# Estilo CSS para imitar la hoja de Tom King
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center;
    }
    .kpi-label {color: #8b949e; font-size: 0.8rem; font-weight: bold;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold;}
    .roi-positive {color: #2ea043; font-size: 1.6rem; font-weight: bold;}
    .roi-negative {color: #f87171; font-size: 1.6rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 5px 15px; 
        border-radius: 5px; margin: 20px 0 10px 0; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V11.5)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.caption("Solo muestra activos con LEAPS activos.")

# --- FUNCIONES CORE ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def run_accounting():
    # 1. Obtener Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    profile = r_profile.json()
    acct = profile['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Obtener Posiciones
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': return {}
    if isinstance(positions, dict): positions = [positions]

    # 3. Obtener Historial Extendido (1000 registros para capturar todo el año)
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", params={'limit': 1000}, headers=get_headers())
    history = r_hist.json().get('history', {}).get('event', []) if r_hist.status_code == 200 else []
    if isinstance(history, dict): history = [history]

    # 4. Market Data
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- PROCESAMIENTO ---
    unique_tickers = list(set([get_underlying(p['symbol']) for p in positions]))
    report = {}

    for t in unique_tickers:
        u_price = q_map.get(t, {}).get('last', 0)
        
        # A. Identificar LEAPS
        leaps_list = []
        total_cost = 0
        current_val = 0
        first_purchase_date = datetime.now()

        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) > 0:
                q = q_map.get(p['symbol'], {})
                delta = q.get('greeks', {}).get('delta', 0)
                if delta and abs(delta) > 0.50: # Es un LEAP
                    cost = abs(float(p['cost_basis']))
                    val = float(p['quantity']) * q.get('last', 0) * 100
                    total_cost += cost
                    current_val += val
                    
                    p_date = datetime.strptime(p['date_acquired'][:10], '%Y-%m-%d')
                    if p_date < first_purchase_date: first_purchase_date = p_date

                    leaps_list.append({
                        "Date": p['date_acquired'][:10], "Exp": q.get('expiration_date'),
                        "Strike": q.get('strike'), "Qty": p['quantity'],
                        "Cost": cost, "Market Val": val, "P/L": val - cost
                    })

        # --- FILTRO CRÍTICO: Si no hay LEAPS en este ticker, lo ignoramos ---
        if total_cost == 0:
            continue

        # B. Reconstruir historial de ventas cortas (CC Realizado)
        realized_profit = 0
        history_table = []
        
        # Agrupar historial por símbolo de opción
        valid_trades = [h for h in history if h.get('type') == 'trade' and get_underlying(h.get('symbol','')) == t]
        
        contract_groups = {}
        for h in valid_trades:
            sym = h['symbol']
            if sym not in contract_groups: contract_groups[sym] = []
            contract_groups[sym].append(h)

        for sym, events in contract_groups.items():
            if len(sym) < 6: continue # Ignorar la acción
            
            # Buscar pares STO -> BTC o STO -> Expirado
            events = sorted(events, key=lambda x: x['date'])
            temp_sto = None
            for e in events:
                if e['side'] == 'sell_to_open':
                    temp_sto = e
                elif e['side'] == 'buy_to_close' and temp_sto:
                    # Trade Cerrado
                    p_sto = abs(float(temp_sto['price']))
                    p_btc = abs(float(e['price']))
                    pnl = (p_sto - p_btc) * 100 * abs(float(e['quantity']))
                    realized_profit += pnl
                    
                    d1 = datetime.strptime(temp_sto['date'][:10], '%Y-%m-%d')
                    d2 = datetime.strptime(e['date'][:10], '%Y-%m-%d')
                    
                    history_table.append({
                        "Date STO": d1.strftime('%m/%d/%y'), "Date BTC": d2.strftime('%m/%d/%y'),
                        "Strike": sym[-8:], "STO": p_sto, "BTC": p_btc, "P/L": pnl, "DIT": (d2-d1).days
                    })
                    temp_sto = None

        # C. Identificar Short Activo (Monitor de Jugo)
        active_short = None
        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) < 0:
                q = q_map.get(p['symbol'], {})
                strike = q.get('strike', 0)
                opt_p = q.get('last', 0)
                # Extrínseco = Precio - Max(0, Stock - Strike)
                ext = opt_p - max(0, u_price - strike)
                active_short = {
                    "Strike": strike, "Price": opt_p, "Ext": ext, 
                    "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days
                }

        # D. Cálculos de Resumen
        dit_total = (datetime.now() - first_purchase_date).days
        net_income = (current_val - total_cost) + realized_profit
        roi = (net_income / total_cost * 100) if total_cost > 0 else 0
        roi_anual = (roi / max(1, dit_total)) * 365

        report[t] = {
            "summary": {
                "cost": total_cost, "val": current_val, "realized": realized_profit,
                "net": net_income, "roi": roi, "roi_anual": roi_anual, "dit": dit_total, "u_price": u_price
            },
            "leaps": leaps_list,
            "history": history_table,
            "active": active_short
        }
        
    return report

# --- INTERFAZ ---

if TOKEN:
    if st.button("🚀 ACTUALIZAR REPORTE CONTABLE"):
        data = run_accounting()
        
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["summary"]["u_price"]:.2f})</div>', unsafe_allow_html=True)
                
                s = d['summary']
                # Fila de KPIs
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${s["cost"]:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${s["val"]:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${s["realized"]:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${s["net"]:,.2f}</p><small style="color:#888">DIT: {s["dit"]}</small></div>', unsafe_allow_html=True)
                
                roi_style = "roi-positive" if s["roi"] >= 0 else "roi-negative"
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL / ANUAL</p><p class="{roi_style}">{s["roi"]:.1f}% / {s["roi_anual"]:.1f}%</p></div>', unsafe_allow_html=True)

                # Tabla Core
                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']))

                # Monitor Jugo
                if d['active']:
                    a = d['active']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                    if a['Ext'] < 0.15: st.error("🚨 CRÍTICO: Valor extrínseco muy bajo. ¡Tiempo de rolear!")

                # Historial
                if d['history']:
                    with st.expander(f"📔 Historial de Cortos Cerrados ({ticker})"):
                        st.dataframe(pd.DataFrame(d['history']).style.format({"P/L": "${:,.2f}"}), use_container_width=True)
                
                st.divider()
        else:
            st.warning("No se encontraron campañas PMCC activas (Tickers con Long Calls > 0.5 Delta).")
else:
    st.info("👈 Introduce tu Token en la barra lateral.")
