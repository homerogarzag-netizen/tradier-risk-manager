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
    .roi-val {color: #2ea043; font-size: 1.6rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 5px 15px; 
        border-radius: 5px; margin: 20px 0 10px 0; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (Tom King Edition)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"

# --- FUNCIONES CORE ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def run_accounting():
    # 1. Obtener Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_p_status := r_profile.status_code != 200: return None
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Obtener Posiciones
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]

    # 3. Obtener Historial Extendido (500 registros para capturar trades antiguos)
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", params={'limit': 500}, headers=get_headers())
    history = r_hist.json().get('history', {}).get('event', [])
    if isinstance(history, dict): history = [history]

    # 4. Market Data (Precios y Griegas)
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- PROCESAMIENTO POR ACTIVO ---
    # Identificamos activos que tienen opciones
    tickers = list(set([get_underlying(p['symbol']) for p in positions if len(p['symbol']) > 5]))
    report = {}

    for t in tickers:
        u_price = q_map.get(t, {}).get('last', 0)
        
        # A. CORE POSITION (LEAPS)
        leaps = []
        total_cost = 0
        current_value = 0
        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) > 0:
                q = q_map.get(p['symbol'], {})
                delta = q.get('greeks', {}).get('delta', 0)
                if delta and abs(delta) > 0.50: # Criterio Leaps
                    cost = abs(float(p['cost_basis']))
                    val = float(p['quantity']) * q.get('last', 0) * 100
                    total_cost += cost
                    current_value += val
                    leaps.append({
                        "Adquirido": p['date_acquired'], "Exp": q.get('expiration_date'),
                        "Strike": q.get('strike'), "Qty": p['quantity'],
                        "Cost": cost, "Value": val, "P/L": val - cost
                    })

        # B. TRADE HISTORY (Emparejamiento STO -> BTC)
        # Filtramos solo trades de este activo que no sean el LEAPS
        valid_hist = [h for h in history if h.get('type') == 'trade' and 'symbol' in h and get_underlying(h['symbol']) == t]
        # Excluir el LEAPS del historial de ventas cortas
        leaps_symbols = [l['Exp'] for l in leaps] # Aproximación
        
        closed_trades = []
        realized_cc_profit = 0
        
        # Agrupar por contrato para buscar pares
        contract_groups = {}
        for h in valid_hist:
            sym = h['symbol']
            if sym not in contract_groups: contract_groups[sym] = []
            contract_groups[sym].append(h)
            
        for sym, events in contract_groups.items():
            # Ordenar por fecha
            events = sorted(events, key=lambda x: x['date'])
            # Buscar Venta (STO) y su posterior Cierre (BTC)
            temp_sto = None
            for e in events:
                if e['side'] == 'sell_to_open':
                    temp_sto = e
                elif e['side'] == 'buy_to_close' and temp_sto:
                    # Par encontrado
                    p_sto = abs(float(temp_sto['price']))
                    p_btc = abs(float(e['price']))
                    qty = abs(float(e['quantity']))
                    pnl = (p_sto - p_btc) * 100 * qty
                    realized_cc_profit += pnl
                    
                    d1 = datetime.strptime(temp_sto['date'][:10], '%Y-%m-%d')
                    d2 = datetime.strptime(e['date'][:10], '%Y-%m-%d')
                    
                    closed_trades.append({
                        "Date STO": d1.strftime('%m/%d/%y'), "Date BTC": d2.strftime('%m/%d/%y'),
                        "Strike": sym[-8:], "STO": p_sto, "BTC": p_btc, "P/L": pnl, "DIT": (d2-d1).days
                    })
                    temp_sto = None

        # C. ACTIVE SHORT (Monitor de Jugo)
        active_short = None
        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) < 0:
                q = q_map.get(p['symbol'], {})
                strike = q.get('strike', 0)
                opt_p = q.get('last', 0)
                # Extrínseco = Precio - Max(0, Stock - Strike)
                ext = opt_p - max(0, u_price - strike)
                active_short = {"Strike": strike, "Price": opt_p, "Ext": ext, "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days}

        # D. RESUMEN FINAL
        leaps_pl = current_value - total_cost
        net_income = leaps_pl + realized_cc_profit
        roi = (net_income / total_cost * 100) if total_cost > 0 else 0
        
        report[t] = {
            "summary": {"cost": total_cost, "val": current_value, "realized": realized_cc_profit, "net": net_income, "roi": roi},
            "leaps": leaps, "history": closed_trades, "active": active_short, "u_price": u_price
        }
    return report

# --- UI TABS ---
tab_risk, tab_acc = st.tabs(["📊 Riesgo & Gráficos", "🏗️ Contabilidad Tom King"])

if TOKEN:
    if st.button("🚀 ACTUALIZAR TODO EL SISTEMA"):
        data = run_accounting()
        if data:
            with tab_acc:
                for ticker, d in data.items():
                    st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["u_price"]:.2f})</div>', unsafe_allow_html=True)
                    s = d['summary']
                    c1, c2, c3, c4, c5 = st.columns(5)
                    c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${s["cost"]:,.2f}</p></div>', unsafe_allow_html=True)
                    c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${s["val"]:,.2f}</p></div>', unsafe_allow_html=True)
                    c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${s["realized"]:,.2f}</p></div>', unsafe_allow_html=True)
                    c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${s["net"]:,.2f}</p></div>', unsafe_allow_html=True)
                    c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="roi-val">{s["roi"]:.1f}%</p></div>', unsafe_allow_html=True)
                    
                    st.write("### 🏛️ CORE POSITION (LEAPS)")
                    st.dataframe(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}), use_container_width=True)
                    
                    if d['active']:
                        a = d['active']
                        st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                        if a['Ext'] < 0.20: st.error("⚠️ TIEMPO DE ROLEAR")
                    
                    if d['history']:
                        with st.expander("📔 Ver Historial de Cortos Cerrados"):
                            st.table(pd.DataFrame(d['history']))
                    st.divider()
        else:
            st.error("Error al obtener datos. Revisa el Token.")
else:
    st.info("👈 Ingresa tu Token para comenzar.")






