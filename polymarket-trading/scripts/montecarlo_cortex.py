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
TRADES_LOG_FILE = os.path.expanduser("~/trades_history.json")

# Globales
SESSION_START_BALANCE = None  # Balance al arrancar; PnL = current - este

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
    global SIMULATION_DATA, SESSION_START_BALANCE
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
            new_balance = round(cash + pos_val, 2)
            SIMULATION_DATA["current_balance"] = new_balance
            SIMULATION_DATA["last_update"] = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
            if SESSION_START_BALANCE is None:
                SESSION_START_BALANCE = new_balance
            SIMULATION_DATA["total_pnl"] = round(new_balance - SESSION_START_BALANCE, 2)
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


def get_resolved_trades(limit=25):
    """Lee trades_history.json y devuelve los últimos trades cerrados (ganadores o perdedores)."""
    try:
        if not os.path.exists(TRADES_LOG_FILE):
            return []
        with open(TRADES_LOG_FILE, "r") as f:
            history = json.load(f)
        resolved = [t for t in history if t.get("resolved") is True]
        resolved.sort(key=lambda x: x.get("resolved_at") or x.get("timestamp") or "", reverse=True)
        return resolved[:limit]
    except Exception:
        return []


def _render_closed_trades(trades):
    """Genera HTML para la sección de trades cerrados (ganadores en verde, perdedores en rojo)."""
    if not trades:
        return html.Div("Sin trades cerrados aún. Se actualizan cada 5 min.", className="tech-gray", style={"fontSize": "10px"})
    rows = []
    for t in trades:
        ts = (t.get("resolved_at") or t.get("timestamp") or "")[:16].replace("T", " ")
        if ts.endswith("Z"):
            ts = ts[:-1]
        market = t.get("market", "—")
        side = t.get("side", "—")
        inv = t.get("investment", 0)
        won = t.get("won", False)
        pnl = t.get("pnl", 0)
        result = "GANÓ" if won else "PERDIÓ"
        result_color = "var(--accent-emerald)" if won else "var(--accent-red)"
        pnl_color = "var(--accent-emerald)" if pnl >= 0 else "var(--accent-red)"
        pnl_str = f"${pnl:+.2f}" if pnl is not None else "—"
        rows.append(html.Tr([
            html.Td(ts, style={"padding": "2px 6px", "color": "var(--text-dim)"}),
            html.Td(market, style={"padding": "2px 6px"}),
            html.Td(side, style={"padding": "2px 6px"}),
            html.Td(f"${inv:.2f}", style={"padding": "2px 6px"}),
            html.Td(result, style={"padding": "2px 6px", "color": result_color, "fontWeight": "600"}),
            html.Td(pnl_str, style={"padding": "2px 6px", "color": pnl_color}),
        ]))
    return html.Table([
        html.Thead(html.Tr([
            html.Th("Hora", style={"padding": "2px 6px", "textAlign": "left", "fontSize": "9px"}),
            html.Th("Mkt", style={"padding": "2px 6px", "fontSize": "9px"}),
            html.Th("Lado", style={"padding": "2px 6px", "fontSize": "9px"}),
            html.Th("$", style={"padding": "2px 6px", "fontSize": "9px"}),
            html.Th("Resultado", style={"padding": "2px 6px", "fontSize": "9px"}),
            html.Th("PnL", style={"padding": "2px 6px", "fontSize": "9px"}),
        ]), style={"borderBottom": "1px solid var(--border-subtle)"}),
        html.Tbody(rows)
    ], style={"width": "100%", "borderCollapse": "collapse"})


# ── DASH APP ──────────────────────────────────────────────────────────────
app = dash.Dash(__name__, external_stylesheets=[
    dbc.themes.CYBORG,
    "https://fonts.googleapis.com/css2?family=Orbitron:wght@400;600;800&family=JetBrains+Mono:wght@400;600&display=swap"
])

app.index_string = '''
<!DOCTYPE html>
<html>
    <head>
        {%metas%}
        <title>Monte Carlo Sniper · Polymarket</title>
        {%favicon%}
        {%css%}
        <style>
            :root {
                --bg-deep: #030308;
                --bg-card: #0a0a12;
                --border-subtle: rgba(0, 212, 255, 0.15);
                --accent-cyan: #00d4ff;
                --accent-emerald: #00ff88;
                --accent-red: #ff3366;
                --accent-purple: #a855f7;
                --text-dim: #6b7280;
                --text-bright: #f1f5f9;
            }
            body { background: var(--bg-deep) !important; color: var(--text-bright); font-family: 'JetBrains Mono', monospace; margin: 0; overflow-x: hidden; }
            .header-bar {
                border-bottom: 1px solid var(--border-subtle);
                padding: 14px 28px;
                font-size: 11px;
                letter-spacing: 4px;
                color: var(--text-dim);
                background: linear-gradient(180deg, rgba(0,212,255,0.03) 0%, transparent 100%);
                font-family: 'Orbitron', sans-serif;
            }
            .header-bar .brand { color: var(--accent-cyan); font-weight: 800; text-shadow: 0 0 20px rgba(0,212,255,0.3); }
            .sidebar {
                border-right: 1px solid var(--border-subtle);
                height: 100vh;
                padding: 24px;
                background: var(--bg-card);
            }
            .metric-box {
                border: 1px solid var(--border-subtle);
                padding: 18px;
                margin-bottom: 16px;
                background: linear-gradient(135deg, rgba(0,212,255,0.02) 0%, transparent 50%);
                border-radius: 8px;
                transition: box-shadow 0.3s, border-color 0.3s;
            }
            .metric-box:hover { border-color: rgba(0,212,255,0.25); box-shadow: 0 0 24px rgba(0,212,255,0.06); }
            .tech-green, .accent-emerald { color: var(--accent-emerald); }
            .tech-red { color: var(--accent-red); }
            .tech-cyan { color: var(--accent-cyan); }
            .tech-gray { color: var(--text-dim); }
            .tech-white { color: var(--text-bright); }
            .label-small { font-size: 9px; color: var(--text-dim); letter-spacing: 2px; margin-bottom: 6px; display: block; text-transform: uppercase; }
            .pulse-live { animation: pulse-glow 2s ease-in-out infinite; }
            @keyframes pulse-glow { 0%, 100% { opacity: 1; text-shadow: 0 0 12px rgba(0,255,136,0.4); } 50% { opacity: 0.85; text-shadow: 0 0 20px rgba(0,255,136,0.6); } }
            .flicker { animation: flicker 2.5s infinite; }
            @keyframes flicker { 0%, 100% { opacity: 0.8; } 50% { opacity: 1; } }
            ::-webkit-scrollbar { width: 4px; }
            ::-webkit-scrollbar-track { background: var(--bg-deep); }
            ::-webkit-scrollbar-thumb { background: var(--accent-cyan); border-radius: 2px; opacity: 0.5; }
            .grid-overlay {
                position: fixed; top: 0; left: 0; width: 100%; height: 100%;
                pointer-events: none;
                background-image: linear-gradient(rgba(0,212,255,0.02) 1px, transparent 1px), linear-gradient(90deg, rgba(0,212,255,0.02) 1px, transparent 1px);
                background-size: 24px 24px;
                opacity: 0.6;
            }
            .prob-gauge { font-family: 'Orbitron', sans-serif; font-weight: 800; font-size: 1.8rem; }
            .chart-title { font-family: 'Orbitron', sans-serif; font-size: 10px; color: var(--text-dim); letter-spacing: 2px; margin-bottom: 8px; }
        </style>
    </head>
    <body>
        <div class="grid-overlay"></div>
        <div class="header-bar d-flex justify-content-between align-items-center">
            <div><span class="brand">MONTE CARLO SNIPER</span> · Polymarket · <span class="tech-white">LIVE ENGINE</span></div>
            <div>[ <span class="tech-green flicker">● RUNNING</span> ] <span id="header-time">''' + datetime.now().strftime("%H:%M:%S") + '''</span></div>
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
                    html.Span("WALLET", className="label-small"),
                    html.H2("creado por MARXMAD", className="tech-cyan mb-4", style={"fontFamily": "Orbitron", "fontSize": "1.1rem", "fontWeight": "600"}),
                    html.Div([
                        html.Span("BALANCE (USDC)", className="label-small"),
                        html.H4("$0.00", id="account-val", className="accent-emerald pulse-live"),
                        html.Small("Session PnL: $0.00", id="session-pnl", className="tech-gray")
                    ], className="metric-box"),
                    html.Div([
                        html.Span("CONFIDENCE", className="label-small"),
                        html.H5("Monte Carlo", className="tech-white"),
                        dcc.Graph(id='mini-sparkline', config={'displayModeBar': False}, style={'height': '48px', 'marginTop': '8px'})
                    ], className="metric-box"),
                    html.Div([
                        html.Span("ENGINE", className="label-small"),
                        html.Div(style={'width': '100%', 'height': '6px', 'background': 'rgba(0,212,255,0.1)', 'marginTop': '10px', 'borderRadius': '3px'}, children=[
                            html.Div(style={'width': '100%', 'height': '100%', 'background': 'linear-gradient(90deg, #00d4ff, #00ff88)', 'borderRadius': '3px'})
                        ], className="mb-2"),
                        html.Small("GBM · 10k paths", className="tech-gray")
                    ], className="metric-box"),
                ], className="sidebar")
            ], width=2, style={'padding': 0}),

            # ══ MAIN DECK (CENTER) ══
            dbc.Col([
                html.Div([
                    dbc.Row([
                        dbc.Col([
                            html.Span("PROB YES (MC)", className="label-small"),
                            html.H3("0.0%", id="main-prob", className="accent-emerald prob-gauge")
                        ], width=4),
                        dbc.Col([
                            html.Span("SLIPPAGE", className="label-small"),
                            html.H3("0.0%", id="main-impact", className="tech-white")
                        ], width=4),
                        dbc.Col([
                            html.Span("TIME TO CLOSE", className="label-small"),
                            html.H3("—", id="main-decay", className="tech-cyan")
                        ], width=4),
                    ], className="py-4 mb-4 mx-3", style={"borderBottom": "1px solid var(--border-subtle)"}),
                    html.Div([
                        html.Span(id="mkt-title", className="chart-title d-block mx-3"),
                        dcc.Graph(id='main-cortex-graph', config={'displayModeBar': False}, style={'height': '520px'})
                    ])
                ])
            ], width=7),

            # ══ RIGHT PANEL ══
            dbc.Col([
                html.Div([
                    html.Span("PRICE DISTRIBUTION AT EXPIRY", className="label-small"),
                    html.Small("10,000 simulations", className="tech-gray d-block mb-2"),
                    dcc.Graph(id='dist-graph', config={'displayModeBar': False}, style={'height': '220px'}),
                    html.Div([
                        html.Span("TRADES CERRADOS", className="label-small mt-4"),
                        html.Small("Ganadores / Perdedores", className="tech-gray d-block mb-2"),
                        html.Div(id="closed-trades", style={
                            'fontSize': '10px',
                            'maxHeight': '220px',
                            'overflowY': 'auto',
                            'border': '1px solid var(--border-subtle)',
                            'borderRadius': '6px',
                            'padding': '8px',
                            'marginBottom': '12px'
                        }),
                    ]),
                    html.Div([
                        html.Span("LIVE LOG", className="label-small mt-2"),
                        html.Div(id="training-log", style={
                            'fontSize': '10px',
                            'height': '320px',
                            'overflowY': 'auto',
                            'color': 'var(--text-dim)',
                            'borderTop': '1px solid var(--border-subtle)',
                            'paddingTop': '12px'
                        })
                    ])
                ], style={'padding': '24px 16px'})
            ], width=3)
        ])
    ], fluid=True),
    dcc.Interval(id='interval-update', interval=2000, n_intervals=0)
], style={'backgroundColor': 'var(--bg-deep)'})

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
     Output("session-pnl", "children"),
     Output("closed-trades", "children")],
    [Input('interval-update', 'n_intervals')]
)
def update_cortex(n):
    data = SIMULATION_DATA

    # Sparkline: confidence trend (mock gentle wave)
    t = np.linspace(0, 4 * np.pi, 30)
    spark_y = 50 + 15 * np.sin(t + n * 0.1) + 5 * np.random.randn(30)
    spark_y = np.clip(spark_y, 30, 70)
    spark = go.Figure(go.Scatter(
        y=spark_y, mode='lines',
        line=dict(color='#00ff88', width=2),
        fill='tozeroy',
        fillcolor='rgba(0,255,136,0.15)'
    ))
    spark.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=0, r=0, t=0, b=0),
        xaxis=dict(visible=False, showgrid=False),
        yaxis=dict(visible=False, showgrid=False)
    )

    logs = [html.Div(it["msg"], style={'marginBottom': '6px', 'fontFamily': 'JetBrains Mono'}) for it in data["neural_log"]]

    if len(data["paths"]) == 0:
        idle_fig = go.Figure()
        idle_fig.add_annotation(
            text="Running Monte Carlo simulation · Scanning Polymarket 5M markets...",
            xref="paper", yref="paper", x=0.5, y=0.5, showarrow=False,
            font=dict(size=14, color="#6b7280", family="Orbitron")
        )
        idle_fig.update_layout(
            paper_bgcolor="#030308", plot_bgcolor="#030308",
            margin=dict(l=40, r=40, t=40, b=40),
            xaxis=dict(showgrid=True, gridcolor='rgba(0,212,255,0.08)', color='#6b7280'),
            yaxis=dict(showgrid=True, gridcolor='rgba(0,212,255,0.08)', color='#6b7280')
        )
        closed = get_resolved_trades()
        closed_ui = _render_closed_trades(closed)
        return idle_fig, idle_fig, spark, "Waiting for market signal...", "—", "—", "—", logs, f"${data['current_balance']:,.2f}", f"Session PnL: ${data['total_pnl']:+.2f}", closed_ui

    paths = np.array(data["paths"])
    target = data["target_price"]
    current_px = data["current_price"]
    n_steps = paths.shape[1]
    x_steps = np.arange(n_steps)

    fig = go.Figure()

    # Split paths into above/below target for color
    wins = paths[:, -1] > target
    for i in range(len(paths)):
        is_win = wins[i]
        opacity = 0.08 + 0.12 * (i / max(len(paths), 1))
        color = f"rgba(0,255,136,{opacity})" if is_win else f"rgba(255,51,102,{opacity})"
        fig.add_trace(go.Scatter(
            x=x_steps, y=paths[i],
            mode='lines',
            line=dict(color=color, width=1.2),
            showlegend=False,
            hoverinfo='skip'
        ))

    # Target line with "glow" (wider faint line behind)
    fig.add_trace(go.Scatter(
        x=x_steps, y=[target] * n_steps,
        mode='lines',
        line=dict(color='rgba(255,51,102,0.4)', width=8),
        showlegend=False, hoverinfo='skip'
    ))
    fig.add_trace(go.Scatter(
        x=x_steps, y=[target] * n_steps,
        mode='lines',
        line=dict(color='#ff3366', width=2, dash='dash'),
        name='Target price',
        hoverinfo='skip'
    ))

    # Current price marker (start of paths)
    fig.add_trace(go.Scatter(
        x=[0], y=[current_px],
        mode='markers',
        marker=dict(color='#00d4ff', size=14, symbol='diamond', line=dict(color='#fff', width=1)),
        name='Spot',
        hovertext=f'Spot: ${current_px:,.2f}'
    ))

    fig.update_layout(
        paper_bgcolor="#030308",
        plot_bgcolor="#030308",
        margin=dict(l=50, r=30, t=30, b=40),
        xaxis=dict(
            showgrid=True, gridcolor='rgba(0,212,255,0.06)', zeroline=False, color='#6b7280',
            title=dict(text='Simulation step', font=dict(size=10, color='#6b7280'))
        ),
        yaxis=dict(
            showgrid=True, gridcolor='rgba(0,212,255,0.06)', zeroline=False, color='#6b7280',
            title=dict(text='Price (USD)', font=dict(size=10, color='#6b7280')),
            tickformat='$,.0f'
        ),
        legend=dict(orientation='h', yanchor='bottom', y=1.02, xanchor='right', x=1, font=dict(size=9, color='#6b7280'))
    )

    # Distribution: histogram with gradient feel + vertical line
    final_prices = np.array(data["final_prices"])
    counts, bin_edges = np.histogram(final_prices, bins=40)
    bin_centers = (bin_edges[:-1] + bin_edges[1:]) / 2
    colors = np.where(bin_centers > target, 'rgba(0,255,136,0.5)', 'rgba(255,51,102,0.4)')
    hist = go.Figure(go.Bar(
        x=bin_centers, y=counts,
        marker_color=colors,
        marker_line_width=0,
        width=(bin_edges[1] - bin_edges[0]) * 0.9,
        name='Paths'
    ))
    hist.add_vline(x=target, line_dash="dash", line_color="#ff3366", line_width=2, annotation_text="TARGET", annotation_position="top")
    hist.update_layout(
        paper_bgcolor="#030308",
        plot_bgcolor="#030308",
        margin=dict(l=35, r=20, t=25, b=35),
        xaxis=dict(visible=True, color='#6b7280', tickformat='$,.0f', showgrid=False),
        yaxis=dict(visible=True, color='#6b7280', title='Count', showgrid=True, gridcolor='rgba(0,212,255,0.06)'),
        showlegend=False,
        bargap=0.05
    )

    time_left = data["time_left"]
    time_str = f"{int(time_left // 60)}m {int(time_left % 60)}s" if time_left >= 60 else f"{time_left:.0f}s"

    closed = get_resolved_trades()
    closed_ui = _render_closed_trades(closed)

    return (
        fig, hist, spark,
        f"{data['ticker']} · {data['market'][:50]}{'...' if len(data['market']) > 50 else ''}",
        f"{data['prob_yes']:.1%}",
        f"{data['price_impact']:.2%}",
        time_str,
        logs,
        f"${data['current_balance']:,.2f}",
        f"Session PnL: ${data['total_pnl']:+.2f}",
        closed_ui
    )

if __name__ == "__main__":
    t = threading.Thread(target=neural_engine_loop, daemon=True)
    t.start()
    app.run(debug=False, host="0.0.0.0", port=8050)
