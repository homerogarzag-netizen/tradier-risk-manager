import streamlit as st
import pandas as pd
import numpy as np
import requests
import re
from datetime import datetime

# --- CONFIGURACIÓN ---
st.set_page_config(layout="wide", page_title="PMCC Forensic V14", page_icon="🧾")

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
    .section-header {background-color: #238636; color: white; padding: 8px 15px; border-radius: 5px; margin: 20px 0; font-weight: bold;}
    </style>
""", unsafe_allow_html=True)

st.title("🏗️ PMCC Forensic Accountant (V14 - Deep Scan)")

with st.sidebar:
    st.header("🔑 Conexión")
    TOKEN = st.text_input("Tradier Token", type="password")
    env_mode = st.radio("Entorno", ["Producción", "Sandbox"])
    BASE_URL = "https://api.tradier.com/v1" if env_mode == "Producción" else "https://sandbox.tradier.com/v1"
    st.divider()
    st.warning("Escarbando en múltiples páginas del historial para encontrar trades antiguos.")

def get_headers(): return {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def get_underlying(symbol):
    if not symbol or len(symbol) < 6: return symbol
    match = re.match(r"([A-Z]+)", symbol)
    return match.group(1) if match else symbol

# --- MOTOR DE DATOS CON PAGINACIÓN ---

def run_deep_scan():
    # 1. Obtener Cuenta
    r_profile = requests.get(f"{BASE_URL}/user/profile", headers=get_headers())
    if r_profile.status_code != 200: return None
    acct = r_profile.json()['profile']['account']
    acct_id = acct[0]['account_number'] if isinstance(acct, list) else acct['account_number']

    # 2. Posiciones Actuales
    r_pos = requests.get(f"{BASE_URL}/accounts/{acct_id}/positions", headers=get_headers())
    positions = r_pos.json().get('positions', {}).get('position', [])
    if not positions or positions == 'null': positions = []
    if isinstance(positions, dict): positions = [positions]
    current_symbols = [p['symbol'] for p in positions]

    # 3. ESCANEO MULTIPÁGINA (Fundamental)
    all_history = []
    # Escaneamos 5 páginas de 100 registros c/u (500 eventos)
    for page in range(1, 6):
        r_hist = requests.get(
            f"{BASE_URL}/accounts/{acct_id}/history", 
            params={'limit': 100, 'page': page, 'start': '2024-01-01'}, 
            headers=get_headers()
        )
        page_events = r_hist.json().get('history', {}).get('event', [])
        if not page_events: break
        if isinstance(page_events, dict): page_events = [page_events]
        all_history.extend(page_events)

    # 4. Market Data
    all_syms = list(set([p['symbol'] for p in positions] + [get_underlying(p['symbol']) for p in positions] + ["SPY"]))
    r_q = requests.get(f"{BASE_URL}/markets/quotes", params={'symbols': ",".join(all_syms), 'greeks': 'true'}, headers=get_headers())
    q_map = {q['symbol']: q for q in r_q.json().get('quotes', {}).get('quote', [])} if r_q else {}

    # --- PROCESAMIENTO ---
    report = {}
    
    # Paso A: Crear reportes solo para activos con LEAPS comprados
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        q_data = q_map.get(sym, {})
        delta = q_data.get('greeks', {}).get('delta', 0)
        
        if float(p['quantity']) > 0 and delta and abs(delta) > 0.50:
            if u_sym not in report:
                report[u_sym] = {"leaps": [], "realized": 0.0, "audit": [], "active_short": None, "spot": q_map.get(u_sym, {}).get('last', 0)}
            
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

    # Paso B: Auditoría de Flujo de Caja (Cualquier opción cerrada o expirada)
    for h in all_history:
        # Buscamos eventos de tipo 'trade' u 'option' (expiraciones)
        if h.get('type') in ['trade', 'option'] and 'symbol' in h:
            sym = h['symbol']
            u_sym = get_underlying(sym)
            
            # Solo si pertenece a uno de nuestros activos PMCC y es una opción
            if u_sym in report and len(sym) > 6:
                side = str(h.get('side', '')).lower()
                price = abs(float(h.get('price', 0)))
                qty = abs(float(h.get('quantity', 0)))
                
                # Definir impacto: Vender es +, Comprar es -
                cash = 0
                if 'sell' in side or h.get('type') == 'option': # Las expiraciones son crédito
                    cash = price * qty * 100
                if 'buy' in side:
                    cash = -(price * qty * 100)

                # Si la opción ya no está abierta, el flujo es REALIZADO
                if sym not in current_symbols:
                    report[u_sym]['realized'] += cash
                    report[u_sym]['audit'].append({
                        "Fecha": h['date'][:10], "Op": side.upper(),
                        "Strike": sym[-8:], "Cash": cash
                    })

    # Paso C: Identificar Short Activo
    for p in positions:
        sym = p['symbol']
        u_sym = get_underlying(sym)
        if u_sym in report and float(p['quantity']) < 0:
            q = q_map.get(sym, {})
            strike = q.get('strike', 0)
            opt_p = q.get('last', 0)
            juice = opt_p - max(0, report[u_sym]['spot'] - strike)
            report[u_sym]['active_short'] = {"Strike": strike, "Ext": juice, "DTE": (datetime.strptime(q['expiration_date'], '%Y-%m-%d') - datetime.now()).days}

    return report

# --- INTERFAZ ---

if TOKEN:
    if st.button("🚀 INICIAR ESCANEO PROFUNDO"):
        data = run_deep_scan()
        if data:
            for ticker, d in data.items():
                st.markdown(f'<div class="section-header">ACTIVO: {ticker}</div>', unsafe_allow_html=True)
                
                t_cost = sum([l['Cost'] for l in d['leaps']])
                t_val = sum([l['Value'] for l in d['leaps']])
                realized = d['realized']
                net_inc = (t_val - t_cost) + realized
                roi = (net_inc / t_cost * 100) if t_cost > 0 else 0
                
                c1, c2, c3, c4, c5 = st.columns(5)
                c1.markdown(f'<div class="summary-card"><p class="kpi-label">COSTO LEAPS</p><p class="kpi-value">${t_cost:,.2f}</p></div>', unsafe_allow_html=True)
                c2.markdown(f'<div class="summary-card"><p class="kpi-label">VALOR ACTUAL</p><p class="kpi-value">${t_val:,.2f}</p></div>', unsafe_allow_html=True)
                c3.markdown(f'<div class="summary-card"><p class="kpi-label">CC REALIZADO</p><p class="kpi-value" style="color:#4ade80">${realized:,.2f}</p></div>', unsafe_allow_html=True)
                c4.markdown(f'<div class="summary-card"><p class="kpi-label">NET INCOME</p><p class="kpi-value">${net_inc:,.2f}</p></div>', unsafe_allow_html=True)
                c5.markdown(f'<div class="summary-card"><p class="kpi-label">ROI TOTAL</p><p class="roi-pos">{roi:.1f}%</p></div>', unsafe_allow_html=True)

                st.write("### 🏛️ CORE POSITION")
                st.table(pd.DataFrame(d['leaps']).style.format({"Cost": "${:,.2f}", "Value": "${:,.2f}", "P/L": "${:,.2f}"}))

                if d['active_short']:
                    a = d['active_short']
                    st.write(f"### 🥤 MONITOR DE JUGO: Strike {a['Strike']} | DTE: {a['DTE']} | **Extrínseco: ${a['Ext']:.2f}**")

                if d['audit']:
                    with st.expander(f"📔 Historial de Auditoría ({ticker})"):
                        st.table(pd.DataFrame(d['audit']).sort_values("Fecha", ascending=False))
                
                st.divider()
        else:
            st.error("Error al conectar. Verifica el Token.")
else:
    st.info("👈 Ingresa tu Token.")







