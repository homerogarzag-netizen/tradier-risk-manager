import streamlit as st
import pandas as pd
import requests
import re
from datetime import datetime
import numpy as np

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
    .section-header {
        background-color: #238636; color: white; padding: 5px 15px; 
        border-radius: 5px; margin: 20px 0 10px 0; font-weight: bold;
    }
    .table-container {margin-bottom: 30px;}
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Credenciales")
    TOKEN = st.text_input("Tradier Token", type="password")
    BASE_URL = "https://api.tradier.com/v1"
    st.divider()
    st.info("Este dashboard recrea la metodología contable de Tom King integrando datos en tiempo real.")

# --- FUNCIONES API ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def fetch_tradier(endpoint, params=None):
    r = requests.get(f"{BASE_URL}{endpoint}", params=params, headers=get_headers())
    return r.json() if r.status_code == 200 else None

def get_underlying(symbol):
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

# --- MOTOR DE CONTABILIDAD ---

def process_pmcc_logic():
    # 1. Obtener Perfil y Cuenta
    profile = fetch_tradier("/user/profile")
    if not profile: return None
    acct_id = profile['profile']['account']['account_number'] if isinstance(profile['profile']['account'], dict) else profile['profile']['account'][0]['account_number']

    # 2. Obtener Posiciones Actuales
    pos_data = fetch_tradier(f"/accounts/{acct_id}/positions")
    positions = pos_data.get('positions', {}).get('position', [])
    if isinstance(positions, dict): positions = [positions]
    
    # 3. Obtener Historial (Últimos 90 días para cerrar el ciclo de Tom King)
    hist_data = fetch_tradier(f"/accounts/{acct_id}/history", params={'limit': 100})
    history = hist_data.get('history', {}).get('event', []) if hist_data else []

    # 4. Obtener Precios y Griegas en Lote
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions]))
    quotes_data = fetch_tradier("/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'})
    q_map = {q['symbol']: q for q in quotes_data.get('quotes', {}).get('quote', [])} if quotes_data else {}

    # --- PROCESAMIENTO POR TICKER ---
    tickers = list(set([get_underlying(p['symbol']) for p in positions if len(p['symbol']) > 5]))
    
    final_report = {}

    for t in tickers:
        # Filtrar info del activo
        u_price = q_map.get(t, {}).get('last', 0)
        
        # A. Identificar CORE POSITION (LEAPS)
        core_positions = []
        total_leaps_cost = 0
        current_leaps_value = 0
        
        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) > 0:
                q = q_map.get(p['symbol'], {})
                exp = q.get('expiration_date', 'N/A')
                delta = q.get('greeks', {}).get('delta', 0)
                
                # Criterio LEAPS: Más de 180 días o Delta > 0.70
                if delta and abs(delta) > 0.60:
                    cost = abs(float(p['cost_basis']))
                    val = float(p['quantity']) * q.get('last', 0) * 100
                    total_leaps_cost += cost
                    current_leaps_value += val
                    
                    core_positions.append({
                        "Date": p['date_acquired'],
                        "Exp": exp,
                        "Strike": q.get('strike'),
                        "QTY": p['quantity'],
                        "Cost": f"${cost:,.2f}",
                        "Market Val": f"${val:,.2f}",
                        "P/L Latente": val - cost
                    })

        # B. Reconstruir Historial Cerrado (CC P/L)
        closed_trades = []
        realized_profit = 0
        
        # Agrupar historial por contrato para encontrar pares STO/BTC
        contract_hist = {}
        for h in history:
            if h.get('type') == 'trade' and get_underlying(h['symbol']) == t:
                sym = h['symbol']
                if sym not in contract_hist: contract_hist[sym] = []
                contract_hist[sym].append(h)
        
        for sym, events in contract_hist.items():
            # Buscamos pares (Venta de apertura y Compra de cierre)
            sto = [e for e in events if e['side'] == 'sell_to_open']
            btc = [e for e in events if e['side'] == 'buy_to_close']
            
            if sto and btc:
                for s, b in zip(sto, btc):
                    pnl = (abs(float(s['price'])) - abs(float(b['price']))) * 100 * abs(float(s['quantity']))
                    realized_profit += pnl
                    
                    d1 = datetime.strptime(s['date'].split('T')[0], '%Y-%m-%d')
                    d2 = datetime.strptime(b['date'].split('T')[0], '%Y-%m-%d')
                    
                    closed_trades.append({
                        "Date STO": d1.strftime('%m/%d/%y'),
                        "Date BTC": d2.strftime('%m/%d/%y'),
                        "Strike": q_map.get(sym, {}).get('strike', 'N/A'),
                        "STO Premium": f"${abs(float(s['price'])):.2f}",
                        "BTC Price": f"${abs(float(b['price'])):.2f}",
                        "P/L Realized": pnl,
                        "DIT": (d2 - d1).days
                    })

        # C. Monitor de "Jugo" (Short Call Activo)
        active_short = None
        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) < 0:
                q = q_map.get(p['symbol'], {})
                strike = q.get('strike', 0)
                opt_price = q.get('last', 0)
                
                # Cálculo Extrínseco (Call): Price - Max(0, Stock - Strike)
                intrinsic = max(0, u_price - strike)
                extrinsic = opt_price - intrinsic
                
                active_short = {
                    "Strike": strike,
                    "Price": opt_price,
                    "Extrinsic": extrinsic,
                    "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days,
                    "Status": "ROLL" if extrinsic < 0.20 else "O.K."
                }

        # D. Cálculos de Resumen (Tom King Style)
        net_income = (current_leaps_value - total_leaps_cost) + realized_profit
        roi = (net_income / total_leaps_cost * 100) if total_leaps_cost > 0 else 0
        
        final_report[t] = {
            "summary": {
                "Costo LEAPS": total_leaps_cost,
                "Valor Actual": current_leaps_value,
                "P/L LEAPS": current_leaps_value - total_leaps_cost,
                "Realizado CC": realized_profit,
                "Net Income": net_income,
                "ROI": roi
            },
            "core": core_positions,
            "history": closed_trades,
            "active_short": active_short
        }
        
    return final_report

# --- RENDERIZADO DE INTERFAZ ---

if TOKEN:
    if st.button("🚀 ACTUALIZAR DASHBOARD"):
        report = process_pmcc_logic()
        
        if report:
            for ticker, data in report.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker}</div>', unsafe_allow_html=True)
                
                # 1. Resumen Superior
                s = data['summary']
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO BASE</p><p class="kpi-value">${s["Costo LEAPS"]:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${s["Valor Actual"]:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC P/L (REALIZADO)</p><p class="kpi-value" style="color:#4ade80">${s["Realizado CC"]:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${s["Net Income"]:,.2f}</p></div>', unsafe_allow_html=True)
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="roi-positive">{s["ROI"]:.1f}%</p></div>', unsafe_allow_html=True)
                
                # 2. Core Position
                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(data['core']))
                
                # 3. Monitor de Jugo (Live Short)
                if data['active_short']:
                    ash = data['active_short']
                    st.write("### 🥤 EXTRINSIC TRACKER (ACTIVE SHORT)")
                    color = "red" if ash['Status'] == "ROLL" else "#4ade80"
                    st.markdown(f"""
                        **Strike Actual:** {ash['Strike']} | **DTE:** {ash['DTE']} | 
                        **Valor Extrínseco:** <span style="color:{color}; font-weight:bold;">${ash['Extrinsic']:.2f}</span> | 
                        **Estado:** {ash['Status']}
                    """, unsafe_allow_html=True)
                
                # 4. Bitácora Cerrada
                st.write("### 📔 TRADE HISTORY (CLOSED CC)")
                if data['history']:
                    df_hist = pd.DataFrame(data['history'])
                    st.dataframe(df_hist.style.format({"P/L Realized": "${:,.2f}"}), use_container_width=True)
                else:
                    st.info("No se encontraron trades cerrados recientemente para este activo.")
                    
                st.divider()
        else:
            st.error("No se pudieron obtener datos. Revisa tu token o conexión.")
else:
    st.info("👈 Por favor ingresa tu Token de Tradier para generar el reporte contable.")




