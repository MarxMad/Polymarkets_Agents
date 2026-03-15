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

import dash
from dash import dcc, html, dash_table
from dash.dependencies import Input, Output, State
import plotly.graph_objects as go
import dash_bootstrap_components as dbc
from plotly.subplots import make_subplots

# ═══════════════════════════════════════════════════════════════════
# 🧠 MARXMAD // NEURAL CORTEX V3.0 - STOCHASTIC AI INTERFACE
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
    "last_update": "N/A"
}

SIMULACIONES_GRAPH = 80
BINANCE_TICKERS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

def add_log(msg):
    global SIMULATION_DATA
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "msg": f"[{timestamp}] {msg}"}
    SIMULATION_DATA["neural_log"] = ([entry] + SIMULATION_DATA["neural_log"])[:15]

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
    if not asks: return 0.5 # Default high impact
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
    creds = ApiCreds(os.getenv("POLYMARKET_API_KEY"), os.getenv("POLYMARKET_API_SECRET"), os.getenv("POLYMARKET_API_PASSPHRASE"))
    return ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=2, funder=os.getenv("PROXY_ADDRESS", ""))

def run_montecarlo_engine():
    global SIMULATION_DATA
    client = get_clob_client()
    add_log("NEURAL CORTEX STARTUP SEQUENCE...")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            url = f"{GAMMA_API}/events?limit=15&tag_id=102892&active=true&closed=false"
            events = requests.get(url, timeout=10).json()
            
            found = False
            for ev in events:
                title = ev.get("title", "").lower()
                ticker = "BTC" if "bitcoin" in title or "btc" in title else "ETH" if "ethereum" in title or "eth" in title else None
                if not ticker: continue
                
                meta = ev.get("eventMetadata") or {}
                price_to_beat_raw = float(meta.get("priceToBeat", 0))
                
                for mkt in ev.get("markets", []):
                    end_dt = datetime.fromisoformat(mkt.get("endDate").replace('Z', '+00:00'))
                    time_left_sec = (end_dt - now).total_seconds()
                    if time_left_sec < 10 or time_left_sec > 1200: continue
                    
                    tids = json.loads(mkt.get("clobTokenIds", "[]"))
                    if len(tids) < 2: continue
                    
                    add_log(f"SYNAPSIS CONNECTED: {ticker} SIGNAL")
                    current_px, vol, drift = get_binance_data(ticker)
                    if not current_px: continue
                    
                    price_to_beat = price_to_beat_raw if price_to_beat_raw > 0 else current_px
                    
                    add_log("COMPUTING 5000 STOCHASTIC FUTURES...")
                    full_paths = calculate_paths(current_px, vol, drift/10, time_left_sec, sims=5000)
                    final_prices = full_paths[:, -1]
                    wins = np.sum(final_prices > price_to_beat)
                    prob_yes = wins / 5000.0
                    
                    book_yes = client.get_order_book(tids[0])
                    ask_yes = min([float(a.price) for a in book_yes.asks]) if book_yes.asks else 0.99
                    edge = prob_yes - ask_yes
                    
                    # Impacto sobre una apuesta de $25 USD
                    impact = calculate_impact(book_yes.asks, 25.0)
                    add_log(f"MODEL OUTPUT: {prob_yes:.1%} (EDGE: {edge:+.1%})")
                    
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
                    found = True
                    break
                if found: break
            if not found:
                 SIMULATION_DATA["market"] = "WAITING FOR MARKET SIGNAL..."
                 SIMULATION_DATA["paths"] = []
                
        except Exception as e:
            add_log(f"ERROR: {str(e)}")
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
            body { background-color: #000 !important; color: #fff; font-family: 'JetBrains Mono', monospace; margin: 0; }
            .sidebar { border-right: 1px solid #222; height: 100vh; padding: 20px; font-size: 11px; }
            .header-bar { border-bottom: 1px solid #222; padding: 10px 20px; font-size: 12px; letter-spacing: 2px; }
            .metric-box { border: 1px solid #222; padding: 15px; margin-bottom: 10px; }
            .neon-green { color: #39ff14; }
            .neon-pink { color: #ff00ff; }
            .neon-cyan { color: #00ffff; }
            .flicker { animation: flicker 3s infinite; }
            @keyframes flicker { 0% { opacity: 0.8; } 50% { opacity: 1; } 100% { opacity: 0.8; } }
            ::-webkit-scrollbar { width: 4px; }
            ::-webkit-scrollbar-track { background: #000; }
            ::-webkit-scrollbar-thumb { background: #333; }
        </style>
    </head>
    <body>
        <div class="header-bar d-flex justify-content-between">
            <div>◆ NEURAL CORTEX <span class="text-muted">SIM v3.0.1</span> | <span class="neon-cyan">TOPOLOGY: SYNAPSE_MESH</span></div>
            <div>[ ACTIVE <span class="neon-green flicker">●</span> ] T_UPD: ''' + datetime.now().strftime("%H:%M:%S") + '''</div>
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

app.layout = dbc.Container([
    dbc.Row([
        # Columna Izquierda: Status & Telemetría
        dbc.Col([
            html.Div([
                html.Small("◆ NETWORK_STATUS", className="text-muted"),
                html.H2("400", className="neon-cyan"),
                html.Small("ACTIVE NEURONS", className="text-muted mb-4 d-block"),
                
                html.Div([
                    html.Small("◆ ACCOUNT_PNL", className="text-muted"),
                    html.H3("+$47,327.91", className="neon-green"),
                    html.Small("REVENUE PROJECTION", className="text-muted d-block small")
                ], className="metric-box"),
                
                html.Div([
                    html.Small("◆ RISK_DENSITY"),
                    dcc.Graph(id='mini-sparkline', config={'displayModeBar': False}, style={'height': '60px'})
                ], className="metric-box"),
                
                html.Div([
                    html.Small("◆ LAYER_ARCHITECTURE", className="text-muted"),
                    html.Table([
                        html.Tr([html.Td("INPUT"), html.Td("784 nodes", className="text-end")]),
                        html.Tr([html.Td("HIDDEN-1"), html.Td("512 nodes", className="text-end")]),
                        html.Tr([html.Td("OUTPUT"), html.Td("SOFTMAX", className="text-end neon-pink")])
                    ], className="w-100 x-small mt-2")
                ], className="metric-box"),
                
            ], className="sidebar")
        ], width=2),

        # Columna Central: Main Graph & Topology
        dbc.Col([
            # Top Stats Section
            dbc.Row([
                dbc.Col([
                    html.Div([
                        html.Small("TARGET_PROBABILITY", className="text-muted"),
                        html.H4("0.0%", id="main-prob", className="neon-green")
                    ])
                ]),
                dbc.Col([
                    html.Div([
                        html.Small("MARKET_IMPACT", className="text-muted"),
                        html.H4("0.0%", id="main-impact", className="neon-pink")
                    ])
                ]),
                dbc.Col([
                    html.Div([
                        html.Small("TIME_DECAY", className="text-muted"),
                        html.H4("0.0s", id="main-decay", className="text-warning")
                    ])
                ])
            ], className="py-4 border-bottom border-secondary mb-3"),

            html.Div([
                html.H6(id="mkt-title", className="mb-3 neon-cyan"),
                dcc.Graph(id='main-cortex-graph', config={'displayModeBar': False}, style={'height': '500px'})
            ])
        ], width=7),

        # Columna Derecha: Distribution & Log
        dbc.Col([
            html.Div([
                html.Small("◆ WEIGHT_DISTRIBUTION", className="text-muted"),
                dcc.Graph(id='dist-graph', config={'displayModeBar': False}, style={'height': '180px'}),
                
                html.Div([
                    html.Small("◆ TRAINING_LOG", className="text-muted d-block mt-4 mb-2"),
                    html.Div(id="training-log", style={'fontSize': '10px', 'height': '400px', 'overflowY': 'auto', 'color': '#888'})
                ])
            ], style={'padding': '20px'})
        ], width=3)
    ])
], fluid=True)

@app.callback(
    [Output('main-cortex-graph', 'figure'),
     Output('dist-graph', 'figure'),
     Output('mini-sparkline', 'figure'),
     Output('mkt-title', 'children'),
     Output('main-prob', 'children'),
     Output('main-impact', 'children'),
     Output('main-decay', 'children'),
     Output('training-log', 'children')],
    [Input('interval-component', 'n_intervals')]
)
def update_cortex(n):
    data = SIMULATION_DATA
    
    # Static Sparkline placeholder
    spark = go.Figure(go.Scatter(y=[1,3,2,4,3], mode='lines', line=dict(color='#00ffff', width=1)))
    spark.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", 
                        margin=dict(l=0,r=0,t=0,b=0), xaxis=dict(visible=False), yaxis=dict(visible=False))

    if not data["paths"]:
        fig = go.Figure().update_layout(template="plotly_dark", paper_bgcolor="#000")
        return fig, fig, spark, "CORE IDLE: WAITING FOR SYNAPSE...", "0.0%", "0.0%", "0.0s", []

    # Main Graph Logic
    paths = data["paths"]
    target = data["target_price"]
    fig = go.Figure()
    
    # Path rendering
    for i in range(len(paths)):
        color = 'rgba(57, 255, 20, 0.08)' if paths[i,-1] > target else 'rgba(255, 0, 255, 0.05)'
        fig.add_trace(go.Scatter(y=paths[i], mode='lines', line=dict(color=color, width=1), showlegend=False))
    
    # Target Line
    fig.add_trace(go.Scatter(y=[target]*len(paths[0]), mode='lines', line=dict(color='#666', width=1, dash='dot')))
    
    fig.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", 
                      margin=dict(l=0,r=0,t=0,b=0), xaxis=dict(showgrid=False), yaxis=dict(gridcolor='#111'))

    # Distribution Logic
    dist = go.Figure(go.Histogram(x=data["final_prices"], nbinsx=30, marker_color='#333', opacity=0.8))
    dist.add_vline(x=target, line_dash="dash", line_color="#ff00ff")
    dist.update_layout(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", 
                       margin=dict(l=10,r=10,t=10,b=10), xaxis=dict(visible=False), yaxis=dict(visible=False))

    log_entries = [html.Div(it["msg"]) for it in data["neural_log"]]
    
    return (
        fig, dist, spark, 
        f"◆ {data['market']}", 
        f"{data['prob_yes']:.1%}", 
        f"{data['price_impact']:.1%}", 
        f"{data['time_left']:.1f}s",
        log_entries
    )

if __name__ == "__main__":
    t = threading.Thread(target=run_montecarlo_engine, daemon=True)
    t.start()
    
    # Intervalo de refresco UI
    app.layout.children.append(dcc.Interval(id='interval-component', interval=2000, n_intervals=0))
    app.run(debug=False, host="0.0.0.0", port=8050)
