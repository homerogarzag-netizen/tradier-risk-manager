import streamlit as st
import pandas as pd
import requests
import re
from datetime import datetime, timedelta

# --- CONFIGURACIÓN DE LA PÁGINA ---
st.set_page_config(layout="wide", page_title="PMCC Master Accountant", page_icon="🧾")

# Estilos CSS Profesionales
st.markdown("""
    <style>
    .stApp {background-color: #0e1117;}
    .summary-card {
        background-color: #161b22; padding: 15px; border-radius: 5px; 
        border: 1px solid #30363d; text-align: center;
    }
    .kpi-label {color: #8b949e; font-size: 0.8rem; font-weight: bold;}
    .kpi-value {color: #ffffff; font-size: 1.4rem; font-weight: bold;}
    .roi-pos {color: #2ea043; font-size: 1.5rem; font-weight: bold;}
    .roi-neg {color: #f87171; font-size: 1.5rem; font-weight: bold;}
    .section-header {
        background-color: #238636; color: white; padding: 5px 15px; 
        border-radius: 5px; margin: 20px 0 10px 0; font-weight: bold;
    }
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Master Accountant (V11.6 - Auditor)")

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

def parse_tradier_date(date_str):
    try: return datetime.strptime(date_str[:10], '%Y-%m-%d')
    except: return datetime.now()

def run_accounting():
    # 1. Obtener Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Obtener Posiciones (Para el CORE y Corto Activo)
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]

    # 3. Obtener Historial Extendido (Pidiendo 1000 registros para profundidad histórica)
    r_hist = requests.get(f"{BASE_URL}/accounts/{acct_id}/history", params={'limit': 1000}, headers=get_headers())
    history = r_hist.json().get('history', {}).get('event', []) if r_hist.status_code == 200 else []
    if isinstance(history, dict): history = [history]

    # 4. Market Data Actual
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- PROCESAMIENTO ---
    unique_tickers = list(set([get_underlying(p['symbol']) for p in positions if len(p['symbol']) > 5]))
    report = {}

    for t in unique_tickers:
        u_price = q_map.get(t, {}).get('last', 0)
        
        # A. CORE POSITION (LEAPS)
        leaps = []
        total_leaps_cost = 0
        current_leaps_val = 0
        earliest_date = datetime.now()

        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) > 0:
                q = q_map.get(p['symbol'], {})
                # Criterio Leaps: Más de 0.50 delta o vencimiento lejano
                if q.get('greeks', {}).get('delta', 0) > 0.50:
                    c = abs(float(p['cost_basis']))
                    v = float(p['quantity']) * q.get('last', 0) * 100
                    total_leaps_cost += c
                    current_leaps_val += v
                    
                    p_date = parse_tradier_date(p['date_acquired'])
                    if p_date < earliest_date: earliest_date = p_date

                    leaps.append({
                        "Date": p_date.strftime('%Y-%m-%d'), "Exp": q.get('expiration_date'),
                        "Strike": q.get('strike'), "Qty": p['quantity'],
                        "Cost": c, "Market Val": v, "P/L": v - c
                    })

        if not leaps: continue # Si no hay LEAPS, ignorar activo

        # B. MOTOR DE HISTORIAL (CC REALIZADO) - LÓGICA DE AUDITORÍA
        realized_profit = 0
        closed_trades = []
        
        # Filtrar trades de este activo que sean opciones
        asset_history = [h for h in history if h.get('type') == 'trade' and 'symbol' in h and get_underlying(h['symbol']) == t and len(h['symbol']) > 6]
        
        # Agrupar por contrato específico
        for sym, events in pd.DataFrame(asset_history).groupby('symbol'):
            events = events.sort_values('date')
            
            sto_event = None
            for idx, row in events.iterrows():
                # Escenario 1: Vendimos para abrir (STO)
                if row['side'] == 'sell_to_open':
                    sto_event = row
                # Escenario 2: Compramos para cerrar (BTC)
                elif row['side'] == 'buy_to_close' and sto_event is not None:
                    p_sto = abs(float(sto_event['price']))
                    p_btc = abs(float(row['price']))
                    qty = abs(float(row['quantity']))
                    pnl = (p_sto - p_btc) * 100 * qty
                    
                    d_sto = parse_tradier_date(sto_event['date'])
                    d_btc = parse_tradier_date(row['date'])
                    
                    closed_trades.append({
                        "Open Date": d_sto.strftime('%m/%d/%y'), "Close Date": d_btc.strftime('%m/%d/%y'),
                        "Strike": sym[-8:], "STO": p_sto, "BTC": p_btc, "P/L": pnl, "DIT": (d_btc - d_sto).days
                    })
                    realized_profit += pnl
                    sto_event = None # Reset para el siguiente ciclo de este contrato

            # Escenario 3: Venta que ya no está en posiciones y no tuvo BTC (Expiración)
            # Buscamos si la opción ya no está abierta
            is_currently_open = any(p['symbol'] == sym for p in positions)
            if sto_event is not None and not is_currently_open:
                # Si vendimos y ya no está en cartera, asumimos que expiró worthless
                p_sto = abs(float(sto_event['price']))
                qty = abs(float(sto_event['quantity']))
                pnl = p_sto * 100 * qty
                d_sto = parse_tradier_date(sto_event['date'])
                
                closed_trades.append({
                    "Open Date": d_sto.strftime('%m/%d/%y'), "Close Date": "EXPIRED",
                    "Strike": sym[-8:], "STO": p_sto, "BTC": 0.00, "P/L": pnl, "DIT": "-"
                })
                realized_profit += pnl

        # C. IDENTIFICAR CORTO ACTIVO (JUGO)
        active_short = None
        for p in positions:
            if get_underlying(p['symbol']) == t and float(p['quantity']) < 0:
                q = q_map.get(p['symbol'], {})
                strike = q.get('strike', 0)
                price = q.get('last', 0)
                ext = price - max(0, u_price - strike)
                active_short = {
                    "Strike": strike, "Price": price, "Ext": ext, 
                    "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days
                }

        # D. RESUMEN FINAL (ESTILO TOM KING)
        leaps_pnl = current_leaps_val - total_leaps_cost
        net_income = leaps_pnl + realized_profit
        roi = (net_income / total_leaps_cost * 100) if total_leaps_cost > 0 else 0
        dit_total = (datetime.now() - earliest_date).days
        roi_anual = (roi / max(1, dit_total)) * 365

        report[t] = {
            "summary": {"cost": total_leaps_cost, "val": current_leaps_val, "realized": realized_profit, "net": net_income, "roi": roi, "roi_a": roi_anual, "dit": dit_total},
            "leaps": leaps, "history": closed_trades, "active": active_short, "spot": u_price
        }

    return report

# --- RENDERIZADO ---
if TOKEN:
    if st.button("🚀 ACTUALIZAR REPORTE CONTABLE"):
        data = run_accounting()
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">ACTIVO: {ticker} (Spot: ${d["spot"]:.2f})</div>', unsafe_allow_html=True)
                
                s = d['summary']
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${s["cost"]:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${s["val"]:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${s["realized"]:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${s["net"]:,.2f}</p><small style="color:#888">DIT: {s["dit"]}</small></div>', unsafe_allow_html=True)
                
                r_style = "roi-pos" if s['roi'] > 0 else "roi-neg"
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL / ANUAL</p><p class="{r_style}">{s["roi"]:.1f}% / {s["roi_a"]:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION (LEAPS)")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Market Val": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active']:
                    a = d['active']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")
                    if a['Ext'] < 0.15: st.error("🚨 CRÍTICO: Poco valor extrínseco. ¡Hora de ROLEAR!")

                if d['history']:
                    with st.expander("📔 Ver Historial de Cortos Cerrados (Bitácora)"):
                        st.table(pd.DataFrame(d['history']).sort_values("Open Date", ascending=False))
                
                st.divider()
        else:
            st.warning("No se encontraron campañas PMCC. Asegúrate que tu LEAPS tenga Delta > 0.50.")
else:
    st.info("👈 Introduce tu Token para auditar la cuenta.")

