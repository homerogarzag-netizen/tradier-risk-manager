import streamlit as st
import yfinance as yf
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master Accountant Pro", page_icon="🧾")

# Estilo CSS Tom King Style
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center; height: 100px;
    }
    .kpi-label {color: #8b949e; font-size: 0.75rem; font-weight: bold; text-transform: uppercase;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold; margin-top: 5px;}
    .roi-pos {color: #2ea043; font-size: 1.4rem; font-weight: bold;}
    .roi-neg {color: #f87171; font-size: 1.4rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 8px 15px; 
        border-radius: 5px; margin: 25px 0 10px 0; font-size: 1.1rem; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V11.8)")

# --- SIDEBAR ---
with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.caption("Analizando historial y balance en tiempo real...")

# --- FUNCIONES CORE ---
def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    if len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

def run_accounting_v11_8():
    # 1. Obtener Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Obtener Posiciones Abiertas
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]

    # 3. Obtener Historial Extendido (1000 registros para capturar todo)
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", params={'limit': 1000}, headers=get_headers())
    history = r_hist.json().get('history', {}).get('event', []) if r_hist.status_code == 200 else []
    if isinstance(history, dict): history = [history]

    # 4. Market Data Actual (Precios e IV)
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- PROCESAMIENTO ---
    report = {}
    # Identificamos activos que tienen LEAPS (Delta > 0.50)
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        # Si es un LEAPS (Long y ITM)
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.50:
            if u_sym not in report:
                report[u_sym] = {"leaps": [], "history": [], "active_short": None, "spot": q_map.get(u_sym, {}).get('last', 0)}
            
            cost = abs(float(p.get('cost_basis', 0)))
            val = float(p['quantity']) * q_data.get('last', 0) * 100
            
            report[u_sym]['leaps'].append({
                "Adquirido": p.get('date_acquired', 'N/A')[:10],
                "Exp": q_data.get('expiration_date'),
                "Strike": q_data.get('strike'),
                "Qty": p['quantity'],
                "Cost": cost,
                "MarketVal": val,
                "PL_Latente": val - cost
            })

    # --- MOTOR DE AUDITORÍA DE TRADES CERRADOS ---
    for t in report.keys():
        # Filtramos historial de este activo
        asset_trades = [h for h in history if h.get('type') == 'trade' and 'symbol' in h and get_underlying(h['symbol']) == t]
        
        realized_cc = 0
        closed_list = []
        
        # Agrupar por contrato específico
        df_h = pd.DataFrame(asset_trades)
        if not df_h.empty:
            for opt_sym, events in df_h.groupby('symbol'):
                if len(opt_sym) < 6: continue # Saltar acciones
                evs = events.sort_values('date')
                
                temp_sto = None
                for _, row in evs.iterrows():
                    if row['side'] == 'sell_to_open':
                        temp_sto = row
                    elif row['side'] == 'buy_to_close' and temp_sto is not None:
                        # Trade Cerrado manualmente
                        pnl = (abs(float(temp_sto['price'])) - abs(float(row['price']))) * 100 * abs(float(row['quantity']))
                        realized_cc += pnl
                        closed_list.append({"STO": temp_sto['date'][:10], "BTC": row['date'][:10], "Strike": opt_sym[-8:], "P/L": pnl})
                        temp_sto = None
                
                # Caso especial: Expiración (Vendido pero ya no está en cartera ni tuvo BTC)
                if temp_sto is not None:
                    is_still_open = any(pos['symbol'] == opt_sym for pos in positions)
                    if not is_still_open:
                        pnl_expired = abs(float(temp_sto['price'])) * 100 * abs(float(temp_sto['quantity']))
                        realized_cc += pnl_expired
                        closed_list.append({"STO": temp_sto['date'][:10], "BTC": "EXPIRED", "Strike": opt_sym[-8:], "P/L": pnl_expired})

        report[t]['realized_cc'] = realized_cc
        report[t]['closed_trades'] = closed_list

        # Identificar el Corto Activo (para el Monitor de Jugo)
        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) < 0:
                q = q_map.get(p['symbol'], {})
                u_p = report[t]['spot']
                strike = q.get('strike', 0)
                opt_price = q.get('last', 0)
                # Extrínseco = Precio Opción - Max(0, Precio Acción - Strike)
                juice = opt_price - max(0, u_p - strike)
                
                report[t]['active_short'] = {
                    "Strike": strike, "Price": opt_price, "Ext": juice, "Qty": p['quantity'],
                    "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days
                }

    return report

# --- INTERFAZ ---
if TOKEN:
    if st.button("🚀 ACTUALIZAR REPORTE CONTABLE"):
        data = run_accounting_v11_8()
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">SYMBOL: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                # Resumen superior
                total_leaps_cost = sum([l['Cost'] for l in d['leaps']])
                total_leaps_val = sum([l['MarketVal'] for l in d['leaps']])
                realized = d['realized_cc']
                net_income = (total_leaps_val - total_leaps_cost) + realized
                roi = (net_income / total_leaps_cost * 100) if total_leaps_cost > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${total_leaps_cost:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${total_leaps_val:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${realized:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${net_income:,.2f}</p></div>', unsafe_allow_html=True)
                
                roi_color = "#4ade80" if roi > 0 else "#f87171"
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="metric-value" style="color:{roi_color}">{roi:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "MarketVal": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    a = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                    if a['Ext'] < 0.15: st.error("🚨 TIEMPO DE ROLEAR: El valor temporal es muy bajo.")

                if d['closed_trades']:
                    with st.expander("📔 Ver Historial Contable (STO / BTC)"):
                        st.table(pd.DataFrame(d['closed_trades']))
                
                st.divider()
        else:
            st.warning("No hay campañas PMCC detectadas (Tickers con Long Calls > 0.5 Delta).")
else:
    st.info("👈 Introduce tu Token en la barra lateral.")



