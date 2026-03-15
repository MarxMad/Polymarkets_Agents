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
from dash import dcc, html
from dash.dependencies import Input, Output
import plotly.graph_objects as go
import dash_bootstrap_components as dbc

# ═══════════════════════════════════════════════════════════════════
# MONTE CARLO SNIPER - VISUALIZER
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("MonteCarloVisualizer")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))
GAMMA_API = "https://gamma-api.polymarket.com"

# Variables globales para compartir datos entre el hilo de cálculo y el Dash app
SIMULATION_DATA = {
    "ticker": "N/A",
    "market": "Waiting for market...",
    "paths": [],
    "target_price": 0,
    "current_price": 0,
    "prob_yes": 0,
    "prob_no": 0,
    "ask_yes": 0,
    "ask_no": 0,
    "edge_yes": 0,
    "edge_no": 0,
    "time_left": 0,
    "last_update": "N/A"
}

SIMULACIONES_GRAPH = 150 # Mostrar menos líneas en la gráfica para rendimiento, calcular 10k internamente

BINANCE_TICKERS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

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
        log.error(f"Error Binance: {e}")
        return None, None, None

def calculate_paths(current_price, volatility, drift, time_to_expiry_sec, sims=SIMULACIONES_GRAPH):
    if time_to_expiry_sec <= 0: time_to_expiry_sec = 1
    T = time_to_expiry_sec / (365 * 24 * 60 * 60)
    steps = max(10, int(time_to_expiry_sec / 5))
    dt = T / steps
    random_shocks = np.random.normal(0, 1, (sims, steps))
    paths = np.zeros((sims, steps + 1))
    paths[:, 0] = current_price
    for t in range(1, steps + 1):
        paths[:, t] = paths[:, t-1] * np.exp((drift - 0.5 * volatility**2) * dt + volatility * np.sqrt(dt) * random_shocks[:, t-1])
    return paths

def get_clob_client():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    creds = ApiCreds(os.getenv("POLYMARKET_API_KEY"), os.getenv("POLYMARKET_API_SECRET"), os.getenv("POLYMARKET_API_PASSPHRASE"))
    return ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=2, funder=os.getenv("PROXY_ADDRESS", ""))

def run_montecarlo_engine():
    global SIMULATION_DATA
    client = get_clob_client()
    while True:
        try:
            now = datetime.now(timezone.utc)
            min_dt = now.strftime('%Y-%m-%dT%H:%M:%SZ')
            max_dt = (now + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ')
            url = f"{GAMMA_API}/events?limit=20&tag_id=102892&active=true&closed=false&end_date_min={min_dt}&end_date_max={max_dt}"
            events = requests.get(url, timeout=10).json()
            
            found = False
            for ev in events:
                title = ev.get("title", "").lower()
                ticker = "BTC" if "bitcoin" in title or "btc" in title else "ETH" if "ethereum" in title or "eth" in title else None
                if not ticker: continue
                meta = ev.get("eventMetadata", {})
                price_to_beat = float(meta.get("priceToBeat", 0))
                if price_to_beat == 0: continue
                
                for mkt in ev.get("markets", []):
                    end_dt = datetime.fromisoformat(mkt.get("endDate").replace('Z', '+00:00'))
                    time_left_sec = (end_dt - now).total_seconds()
                    if time_left_sec < 10 or time_left_sec > 900: continue
                    
                    tids = json.loads(mkt.get("clobTokenIds", "[]"))
                    if len(tids) < 2: continue
                    
                    current_px, vol, drift = get_binance_data(ticker)
                    if not current_px: continue
                    
                    # Calcular simulaciones completas para probabilidad (10,000)
                    full_paths = calculate_paths(current_px, vol, drift/10, time_left_sec, sims=10000)
                    wins = np.sum(full_paths[:, -1] > price_to_beat)
                    prob_yes = wins / 10000.0
                    
                    # Polling a Polymarket
                    book_yes = client.get_order_book(tids[0])
                    ask_yes = min([float(a.price) for a in book_yes.asks]) if book_yes.asks else 0.99
                    ask_no = 1 - ask_yes # Simplificado para visualización
                    
                    # Tomar paths reducidos para la gráfica (150)
                    graph_paths = full_paths[:SIMULACIONES_GRAPH, :]
                    
                    SIMULATION_DATA = {
                        "ticker": ticker,
                        "market": f"{mkt.get('question')} (Target: ${price_to_beat:,.2f})",
                        "paths": graph_paths,
                        "target_price": price_to_beat,
                        "current_price": current_px,
                        "prob_yes": prob_yes,
                        "prob_no": 1.0 - prob_yes,
                        "ask_yes": ask_yes,
                        "ask_no": ask_no,
                        "edge_yes": prob_yes - ask_yes,
                        "time_left": time_left_sec,
                        "last_update": datetime.now().strftime("%H:%M:%S")
                    }
                    found = True
                    break
                if found: break
                
        except Exception as e:
            log.error(f"Engine error: {e}")
        time.sleep(3)

# ── DASH APP ──────────────────────────────────────────────────────────────
app = dash.Dash(__name__, external_stylesheets=[dbc.themes.CYBORG])

app.layout = dbc.Container([
    dbc.Row([
        dbc.Col([
            html.H2("♦ MONTE CARLO ENGINE ♦", className="text-center text-primary mt-3", style={"fontFamily": "monospace"}),
            html.P("Real-time Stochastic Probability Modeling for Polymarket", className="text-center text-secondary mb-4")
        ])
    ]),
    
    dbc.Row([
        dbc.Col([
            dbc.Card([
                dbc.CardBody([
                    html.H5("Live Market Tracking", className="text-info"),
                    html.H4(id="live-market-title", className="mb-3"),
                    html.H6(id="live-time-left", className="text-warning")
                ])
            ])
        ], width=12, className="mb-4")
    ]),
    
    dbc.Row([
        dbc.Col([
            dcc.Graph(id='simulation-graph', config={'displayModeBar': False})
        ], width=8),
        
        dbc.Col([
            dbc.Card([
                dbc.CardBody([
                    html.H5("Probabilities (Math vs Market)", className="text-info"),
                    html.Hr(),
                    html.H6("Math Derived <YES>:", className="mt-3 text-muted"),
                    html.H3(id="stat-math-yes", className="text-success"),
                    
                    html.H6("Market Price <YES>:", className="mt-3 text-muted"),
                    html.H3(id="stat-mkt-yes", className="text-danger"),
                    
                    html.H6("Current Edge:", className="mt-3 text-muted"),
                    html.H3(id="stat-edge-yes", className="text-warning"),
                    
                    html.Hr(),
                    html.P("Simulation active computing GBM on latest volatility.", className="small text-muted")
                ])
            ])
        ], width=4)
    ]),
    
    dcc.Interval(id='interval-component', interval=2000, n_intervals=0)
], fluid=True, style={"padding": "2rem", "fontFamily": "monospace"})

@app.callback(
    [Output('simulation-graph', 'figure'),
     Output('live-market-title', 'children'),
     Output('live-time-left', 'children'),
     Output('stat-math-yes', 'children'),
     Output('stat-mkt-yes', 'children'),
     Output('stat-edge-yes', 'children')],
    [Input('interval-component', 'n_intervals')]
)
def update_graph(n):
    data = SIMULATION_DATA
    
    if len(data.get("paths", [])) == 0:
        fig = go.Figure()
        fig.update_layout(template="plotly_dark", title="Waiting for a valid binary market...")
        return fig, data["market"], "", "0.0%", "$0.00", "0.0%"
    
    paths = data["paths"]
    target = data["target_price"]
    steps = paths.shape[1]
    x_vals = np.arange(steps)
    
    fig = go.Figure()
    
    # Plot simulaciones (líneas finas, verdes si ganan, rojas si pierden)
    for i in range(paths.shape[0]):
        color = 'rgba(0, 255, 0, 0.1)' if paths[i, -1] > target else 'rgba(255, 0, 0, 0.1)'
        fig.add_trace(go.Scatter(x=x_vals, y=paths[i, :], mode='lines', line=dict(color=color, width=1), showlegend=False))
        
    # Plot target line
    fig.add_trace(go.Scatter(x=[0, steps-1], y=[target, target], mode='lines', line=dict(color='yellow', width=2, dash='dash'), name='Strike Price'))
    
    # Plot current price evolution (just one point actually)
    fig.add_trace(go.Scatter(x=[0], y=[data["current_price"]], mode='markers', marker=dict(color='white', size=8), name='Current Price'))
    
    fig.update_layout(
        template="plotly_dark",
        title=f"Asset: {data['ticker']} | {SIMULACIONES_GRAPH} paths shown (10k computed hidden)",
        xaxis_title="Time Steps (-> Expiry)",
        yaxis_title="Underlying Price (USD)",
        height=500,
        margin=dict(l=40, r=40, t=50, b=40)
    )
    
    edge_str = f"{(data['edge_yes']):+.1%}"
    
    return (
        fig, 
        data["market"], 
        f"Time remaining: {data['time_left']:.1f} sec",
        f"{data['prob_yes']:.1%}",
        f"${data['ask_yes']:.2f}",
        edge_str
    )

if __name__ == "__main__":
    t = threading.Thread(target=run_montecarlo_engine, daemon=True)
    t.start()
    # Ejecutamos Dash en el puerto 8050
    app.run_server(debug=False, host="0.0.0.0", port=8050)
