import os
import time
import json
import logging
import math
import numpy as np
import threading
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds
from web3 import Web3

import dash
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output, State
import plotly.graph_objects as go
import dash_bootstrap_components as dbc

# ═══════════════════════════════════════════════════════════════════
# 🧠 MARXMAD // NEURAL CORTEX V3.1 - HIGH-TECH TACTICAL INTERFACE
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("NeuralCortex")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))
GAMMA_API = "https://gamma-api.polymarket.com"

# Globales
SIMULATION_DATA = {
    "ticker": "N/A",
    "market": "INITIALIZING NEURAL NETWORK...",
    "paths": [],
    "final_prices": [],
    "target_price": 0,
    "current_price": 0,
    "prob_yes": 0,
    "ask_yes": 0,
    "edge_yes": 0,
    "price_impact": 0,
    "time_left": 0,
    "neural_log": [],
    "total_pnl": 0.00,
    "current_balance": 11.00,
    "last_update": "N/A"
}

SIMULACIONES_GRAPH = 80
BINANCE_TICKERS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

def add_log(msg):
    global SIMULATION_DATA
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "msg": f"[{timestamp}] {msg}"}
    SIMULATION_DATA["neural_log"] = ([entry] + SIMULATION_DATA["neural_log"])[:20]

def get_binance_data(ticker):
    symbol = BINANCE_TICKERS.get(ticker)
    if not symbol: return None, None, None
    try:
        r_px = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=3).json()
        current_price = float(r_px["price"])
        r_kl = requests.get(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit=60", timeout=3).json()
        closes = [float(k[4]) for k in r_kl]
        returns = np.diff(closes) / closes[:-1]
        volatility = np.std(returns) * np.sqrt(525600)
        drift = np.mean(returns) * 525600
        return current_price, volatility, drift
    except Exception as e:
        return None, None, None

def calculate_paths(current_price, volatility, drift, time_to_expiry_sec, sims=SIMULACIONES_GRAPH):
    if time_to_expiry_sec <= 0: time_to_expiry_sec = 1
    T = time_to_expiry_sec / (365 * 24 * 60 * 60)
    steps = 40
    dt = T / steps
    random_shocks = np.random.normal(0, 1, (sims, steps))
    paths = np.zeros((sims, steps + 1))
    paths[:, 0] = current_price
    for t in range(1, steps + 1):
        paths[:, t] = paths[:, t-1] * np.exp((drift - 0.5 * volatility**2) * dt + volatility * np.sqrt(dt) * random_shocks[:, t-1])
    return paths

def calculate_impact(asks, size_usd):
    if not asks: return 0.5
    total_filled = 0
    total_cost = 0
    remaining = size_usd
    best_ask = float(asks[0].price)
    for ask in asks:
        price = float(ask.price)
        size = float(ask.size)
        available_usd = price * size
        if remaining <= available_usd:
            total_cost += remaining
            total_filled += remaining / price
            remaining = 0
            break
        else:
            total_cost += available_usd
            total_filled += size
            remaining -= available_usd
    if total_filled == 0: return 0
    avg_price = total_cost / total_filled
    return (avg_price - best_ask) / best_ask

def get_clob_client():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    creds = ApiCreds(
        os.getenv("POLYMARKET_API_KEY"),
        os.getenv("POLYMARKET_API_SECRET"),
        os.getenv("POLYMARKET_API_PASSPHRASE")
    )
    proxy = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")
    return ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=2, funder=proxy)

def update_real_balance(client):
    """Actualiza TOTAL_ACCOUNT_VALUE con USDC en wallet + valor posiciones. Funciona desde AWS con múltiples RPCs."""
    global SIMULATION_DATA
    proxy = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")
    usdc_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    request_timeout = 12  # AWS puede ser más lento; evitar timeouts cortos

    # Muchos RPCs públicos para Polygon (desde AWS algunos fallan o bloquean)
    RPC_FALLBACKS = [
        "https://polygon-rpc.com",
        "https://polygon.drpc.org",
        "https://rpc.ankr.com/polygon",
        "https://1rpc.io/matic",
        "https://polygon-bor-rpc.publicnode.com",
        "https://polygon-mainnet.public.blastapi.io",
        "https://polygon.api.onfinality.io/public",
        "https://polygon-public.nodies.app",
        "https://polygon.llamarpc.com",
        "https://polygon-mainnet.core.chainstack.com/archive",
    ]

    abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]

    while True:
        cash = None
        for rpc_url in RPC_FALLBACKS:
            try:
                w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": request_timeout}))
                usdc_contract = w3.eth.contract(address=Web3.to_checksum_address(usdc_address), abi=abi)
                balance_raw = usdc_contract.functions.balanceOf(Web3.to_checksum_address(proxy)).call()
                cash = balance_raw / 1e6
                log.info(f"Balance USDC: ${cash:.2f} (RPC: {rpc_url.split('/')[2][:20]}...)")
                break
            except Exception as e:
                log.debug(f"RPC {rpc_url[:40]}... falló: {e}")
                continue

        pos_val = 0.0
        if cash is not None:
            try:
                url = f"https://gamma-api.polymarket.com/positions?user={proxy.lower()}"
                r = requests.get(url, timeout=request_timeout)
                r.raise_for_status()
                data = r.json()
                pos_val = sum([float(p.get("size", 0)) * float(p.get("price", 0)) for p in data])
            except Exception as e:
                log.debug(f"Gamma positions falló: {e}")
                # Mostramos al menos el cash

        if cash is not None:
            SIMULATION_DATA["current_balance"] = round(cash + pos_val, 2)
            SIMULATION_DATA["last_update"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        else:
            log.warning("Telemetría de balance: todos los RPC fallaron. Reintentando en 30s...")

        time.sleep(30)

def neural_engine_loop():
    global SIMULATION_DATA
    client = get_clob_client()
    
    # Iniciar monitor de balance en paralelo
    threading.Thread(target=update_real_balance, args=(client,), daemon=True).start()
    
    add_log("NEURAL ENGINE INITIALIZED")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Aumentamos el rango de búsqueda para capturar más mercados
            min_dt = now.strftime('%Y-%m-%dT%H:%M:%SZ')
            max_dt = (now + timedelta(minutes=20)).strftime('%Y-%m-%dT%H:%M:%SZ')
            url = f"{GAMMA_API}/events?limit=25&active=true&closed=false&tag_id=102892&end_date_min={min_dt}&end_date_max={max_dt}"
            events = requests.get(url, timeout=10).json()
            
            found = False
            for ev in events:
                title = ev.get("title", "").lower()
                ticker = "BTC" if "bitcoin" in title or "btc" in title else "ETH" if "ethereum" in title or "eth" in title else None
                if not ticker: continue
                
                meta = ev.get("eventMetadata") or {}
                price_to_beat_raw = float(meta.get("priceToBeat", 0))
                
                for mkt in ev.get("markets", []):
                    endDate = mkt.get("endDate")
                    if not endDate: continue
                    end_dt = datetime.fromisoformat(endDate.replace('Z', '+00:00'))
                    time_left_sec = (end_dt - now).total_seconds()
                    
                    if time_left_sec < 5 or time_left_sec > 1200: continue
                    
                    tids = json.loads(mkt.get("clobTokenIds", "[]"))
                    if len(tids) < 2: continue
                    
                    current_px, vol, drift = get_binance_data(ticker)
                    if not current_px: continue
                    
                    price_to_beat = price_to_beat_raw if price_to_beat_raw > 0 else current_px
                    
                    full_paths = calculate_paths(current_px, vol, drift/10, time_left_sec, sims=2000)
                    final_prices = full_paths[:, -1]
                    wins = np.sum(final_prices > price_to_beat)
                    prob_yes = wins / 2000.0
                    
                    try:
                        book_yes = client.get_order_book(tids[0])
                        ask_yes = min([float(a.price) for a in book_yes.asks]) if book_yes.asks else 0.99
                    except:
                        ask_yes = 0.5
                        
                    edge = prob_yes - ask_yes
                    impact = calculate_impact(getattr(book_yes, 'asks', []), 25.0) if 'book_yes' in locals() else 0
                    
                    SIMULATION_DATA.update({
                        "ticker": ticker,
                        "market": f"{mkt.get('question').upper()}",
                        "paths": full_paths[:SIMULACIONES_GRAPH, :],
                        "final_prices": final_prices,
                        "target_price": price_to_beat,
                        "current_price": current_px,
                        "prob_yes": prob_yes,
                        "ask_yes": ask_yes,
                        "edge_yes": edge,
                        "price_impact": impact,
                        "time_left": time_left_sec,
                    })
                    add_log(f"SIGNAL CAPTURED: {ticker} @ {current_px:.0f} | EDGE: {edge:+.1%}")
                    found = True
                    break
                if found: break
            if not found:
                 SIMULATION_DATA["market"] = "WAITING FOR SYNAPTIC SIGNAL..."
                 SIMULATION_DATA["paths"] = []
                
        except Exception as e:
            add_log(f"ERR: {str(e)[:40]}")
        time.sleep(3)

# ── DASH APP ──────────────────────────────────────────────────────────────
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.CYBORG, "https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@100;400;700&display=swap"])

app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>MARXMAD // NEURAL CORTEX</title>
        {%favicon%}
        {%css%}
        <style>
            body { background-color: #000 !important; color: #fff; font-family: 'JetBrains Mono', monospace; margin: 0; overflow-x: hidden; }
            .header-bar { border-bottom: 1px solid #222; padding: 12px 25px; font-size: 11px; letter-spacing: 3px; color: #888; background: #050505; }
            .sidebar { border-right: 1px solid #222; height: 100vh; padding: 25px; background: #000; }
            .metric-box { border: 1px solid #1a1a1a; padding: 15px; margin-bottom: 15px; background: #070707; }
            .tech-green { color: #39ff14; }
            .tech-red { color: #ff3131; }
            .tech-gray { color: #4a4a4a; }
            .tech-white { color: #ffffff; }
            .label-small { font-size: 9px; color: #555; letter-spacing: 1px; margin-bottom: 5px; display: block; }
            .flicker { animation: flicker 2s infinite; }
            @keyframes flicker { 0% { opacity: 0.7; } 50% { opacity: 1; } 100% { opacity: 0.7; } }
            ::-webkit-scrollbar { width: 3px; }
            ::-webkit-scrollbar-track { background: #000; }
            ::-webkit-scrollbar-thumb { background: #222; }
            .grid-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; background-image: radial-gradient(#111 0.5px, transparent 0.5px); background-size: 30px 30px; opacity: 0.3; }
        </style>
    </head>
    <body>
        <div class="grid-overlay"></div>
        <div class="header-bar d-flex justify-content-between align-items-center">
            <div><span class="tech-white">◆ NEURAL CORTEX</span> [VER_3.1.2] // TARGET: <span class="tech-white">STOCHASTIC_GAUSS</span></div>
            <div>[ <span class="tech-green flicker">RUNNING</span> ] T_LOCAL: ''' + datetime.now().strftime("%H:%M:%S") + '''</div>
        </div>
        {%app_entry%}
        <footer>
            {%config%}
            {%scripts%}
            {%renderer%}
        </footer>
    </body>
</html>
'''

app.layout = html.Div([
    dbc.Container([
        dbc.Row([
            # ══ SIDEBAR (LEFT) ══
            dbc.Col([
                html.Div([
                    html.Span("NETWORK_NODE", className="label-small"),
                    html.H2("NCX-01", className="tech-white mb-4"),
                    
                    html.Div([
                        html.Span("TOTAL_ACCOUNT_VALUE", className="label-small"),
                        html.H4("$50.00", id="account-val", className="tech-green"),
                        html.Small("SESSION_PNL: $0.00", id="session-pnl", className="tech-gray")
                    ], className="metric-box"),
                    
                    html.Div([
                        html.Span("MODEL_CONFIDENCE", className="label-small"),
                        html.H5("HIGH_DENSITY", className="tech-white"),
                        dcc.Graph(id='mini-sparkline', config={'displayModeBar': False}, style={'height': '50px'})
                    ], className="metric-box"),
                    
                    html.Div([
                        html.Span("HARDWARE_LOAD", className="label-small"),
                        html.Div(style={'width': '100%', 'height': '4px', 'background': '#111', 'marginTop': '10px'}, children=[
                            html.Div(style={'width': '78%', 'height': '100%', 'background': '#fff'})
                        ], className="mb-2"),
                        html.Small("GPU_TEMP: 42°C", className="tech-gray")
                    ], className="metric-box"),
                    
                ], className="sidebar")
            ], width=2, style={'padding': 0}),

            # ══ MAIN DECK (CENTER) ══
            dbc.Col([
                html.Div([
                    # Telemetry Row
                    dbc.Row([
                        dbc.Col([
                            html.Span("SIGNAL_PROBABILITY", className="label-small"),
                            html.H3("0.0%", id="main-prob", className="tech-green")
                        ], width=4),
                        dbc.Col([
                            html.Span("MARKET_SLIPPAGE", className="label-small"),
                            html.H3("0.0%", id="main-impact", className="tech-white")
                        ], width=4),
                        dbc.Col([
                            html.Span("DECAY_TIMER", className="label-small"),
                            html.H3("0.0s", id="main-decay", className="tech-gray")
                        ], width=4),
                    ], className="py-4 border-bottom border-secondary mb-4 mx-3"),

                    # Main Visualization
                    html.Div([
                        html.Span(id="mkt-title", className="label-small mb-2 mx-3", style={'fontSize': '11px', 'color': '#888'}),
                        dcc.Graph(id='main-cortex-graph', config={'displayModeBar': False}, style={'height': '520px'})
                    ])
                ])
            ], width=7),

            # ══ DATA LOG (RIGHT) ══
            dbc.Col([
                html.Div([
                    html.Span("DISTRIBUTION_CURVE", className="label-small"),
                    dcc.Graph(id='dist-graph', config={'displayModeBar': False}, style={'height': '200px'}),
                    
                    html.Div([
                        html.Span("NEURAL_TRAINING_LOG", className="label-small mt-4"),
                        html.Div(id="training-log", style={
                            'fontSize': '10px', 
                            'height': '420px', 
                            'overflowY': 'auto', 
                            'color': '#666', 
                            'borderTop': '1px solid #111',
                            'paddingTop': '10px'
                        })
                    ])
                ], style={'padding': '25px 15px'})
            ], width=3)
        ])
    ], fluid=True),
    dcc.Interval(id='interval-update', interval=2000, n_intervals=0)
], style={'backgroundColor': '#000'})

@app.callback(
    [Output('main-cortex-graph', 'figure'),
     Output('dist-graph', 'figure'),
     Output('mini-sparkline', 'figure'),
     Output('mkt-title', 'children'),
     Output('main-prob', 'children'),
     Output('main-impact', 'children'),
     Output("main-decay", "children"),
     Output("training-log", "children"),
     Output("account-val", "children"),
     Output("session-pnl", "children")],
    [Input('interval-update', 'n_intervals')]
)
def update_cortex(n):
    data = SIMULATION_DATA
    
    # Sparkline mock
    spark = go.Figure(go.Scatter(y=[2,4,3,5,4,6,5], mode='lines', line=dict(color='#39ff14', width=1.5)))
    spark.update_layout(
        paper_bgcolor="#000", plot_bgcolor="#000",
        margin=dict(l=0,r=0,t=0,b=0),
        xaxis=dict(visible=False, showgrid=False),
        yaxis=dict(visible=False, showgrid=False)
    )

    logs = [html.Div(it["msg"], style={'marginBottom': '4px'}) for it in data["neural_log"]]
    
    if len(data["paths"]) == 0:
        idle_fig = go.Figure()
        idle_fig.update_layout(
            paper_bgcolor="#000", plot_bgcolor="#000",
            xaxis=dict(showgrid=True, gridcolor='#111', color='#333'),
            yaxis=dict(showgrid=True, gridcolor='#111', color='#333')
        )
        return idle_fig, idle_fig, spark, "SEARCHING FOR NEURAL SYNAPSES...", "0.0%", "0.0%", "0.0s", logs, f"${data['current_balance']:,.2f}", f"SESSION_PNL: ${data['total_pnl']:+.2f}"

    # Graph
    paths = data["paths"]
    target = data["target_price"]
    fig = go.Figure()
    
    # Scanning Line logic (visual)
    scan_idx = (n % 40)
    
    for i in range(len(paths)):
        # Tech theme: Mostly white/gray, only ends colored
        is_win = paths[i,-1] > target
        color = 'rgba(57, 255, 20, 0.12)' if is_win else 'rgba(255, 49, 49, 0.08)'
        fig.add_trace(go.Scatter(y=paths[i], mode='lines', line=dict(color=color, width=1), showlegend=False, hoverinfo='none'))
    
    # Strike line
    fig.add_trace(go.Scatter(y=[target]*len(paths[0]), mode='lines', line=dict(color='#333', width=1.5, dash='dot')))
    
    # Current price marker
    fig.add_trace(go.Scatter(x=[0], y=[data["current_price"]], mode='markers', marker=dict(color='#fff', size=10, symbol='square-open')))

    fig.update_layout(
        paper_bgcolor="#000",
        plot_bgcolor="#000",
        margin=dict(l=20,r=20,t=20,b=20),
        xaxis=dict(showgrid=False, zeroline=False, color='#333'),
        yaxis=dict(showgrid=True, gridcolor='#111', zeroline=False, color='#333')
    )

    # Histogram
    hist = go.Figure(go.Histogram(x=data["final_prices"], nbinsx=30, marker_color='#1a1a1a', marker_line_color='#333', marker_line_width=1))
    hist.add_vline(x=target, line_dash="dash", line_color="#ff3131")
    hist.update_layout(
        paper_bgcolor="#000",
        plot_bgcolor="#000",
        margin=dict(l=5,r=5,t=5,b=5),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False)
    )

    logs = [html.Div(it["msg"], style={'marginBottom': '4px'}) for it in data["neural_log"]]
    
    return (
        fig, hist, spark, 
        f"◆ {data['market']}", 
        f"{data['prob_yes']:.1%}", 
        f"{data['price_impact']:.2%}", 
        f"{data['time_left']:.1f}s",
        logs,
        f"${data['current_balance']:,.2f}",
        f"SESSION_PNL: ${data['total_pnl']:+.2f}"
    )

if __name__ == "__main__":
    t = threading.Thread(target=neural_engine_loop, daemon=True)
    t.start()
    app.run(debug=False, host="0.0.0.0", port=8050)
