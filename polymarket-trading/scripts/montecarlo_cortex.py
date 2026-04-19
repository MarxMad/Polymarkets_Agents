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
from dash import dcc, html, dash_table, no_update, callback_context
from dash.dependencies import Input, Output, State
from dash.exceptions import PreventUpdate
import plotly.graph_objects as go
import dash_bootstrap_components as dbc

from straddle_optimizer import load_markets as load_straddle_markets, run_grid_search as run_straddle_grid

# ═══════════════════════════════════════════════════════════════════
# 🧠 MARXMAD // NEURAL CORTEX V3.1 - HIGH-TECH TACTICAL INTERFACE
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("NeuralCortex")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))
GAMMA_API = "https://gamma-api.polymarket.com"
TRADES_LOG_FILE = os.path.expanduser("~/trades_history.json")
STRADDLE_TRADES_LOG_FILE = os.path.expanduser(os.getenv("STRADDLE_TRADES_LOG_FILE", "~/trades_history_straddle.json"))
STRADDLE_SNAPSHOTS_FILE = os.path.expanduser(os.getenv("OB_IN_FILE", "~/orderbook_snapshots.jsonl"))

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
    "ask_no": 0.0,
    "edge_yes": 0,
    "price_impact": 0,
    "time_left": 0,
    "market_id": None,
    "neural_log": [],
    "total_pnl": 0.00,
    "current_balance": 11.00,
    "last_update": "N/A"
}

SIMULACIONES_GRAPH = 80
BINANCE_TICKERS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}
STRADDLE_OPT_CACHE = {"mtime": None, "markets": None, "lines": 0}
PROB_SPARK = []  # últimos prob YES del motor en vivo (mini sparkline)


def _field(**extra):
    """Inputs legibles: texto oscuro sobre fondo claro (no se pierde en el tema CYBORG)."""
    st = {
        "color": "#0f172a",
        "backgroundColor": "#e8edf4",
        "border": "1px solid #64748b",
        "borderRadius": "4px",
        "padding": "5px 8px",
        "fontSize": "12px",
        "fontFamily": "'JetBrains Mono', monospace",
    }
    st.update(extra)
    return st


def add_log(msg):
    global SIMULATION_DATA
    timestamp = datetime.now().strftime("%H:%M:%S")
    entry = {"time": timestamp, "msg": f"[{timestamp}] {msg}"}
    SIMULATION_DATA["neural_log"] = ([entry] + SIMULATION_DATA["neural_log"])[:20]

def get_binance_historical(ticker, iso_str):
    """Precio de apertura del minuto ISO (para strike tipo priceToBeat al inicio del mercado)."""
    try:
        ts = int(datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp() * 1000)
        symbol = BINANCE_TICKERS.get(ticker)
        if not symbol:
            return 0.0
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&startTime={ts}&limit=1"
        r = requests.get(url, timeout=3).json()
        if r and len(r) > 0:
            return float(r[0][1])
        return 0.0
    except Exception:
        return 0.0


def get_binance_data(ticker):
    """Misma lógica que montecarlo_sniper: log-returns, vol/drift acotados."""
    symbol = BINANCE_TICKERS.get(ticker)
    if not symbol:
        return None, None, None
    try:
        r_px = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=3).json()
        current_price = float(r_px["price"])
        r_kl = requests.get(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit=60", timeout=3).json()
        closes = np.array([float(k[4]) for k in r_kl], dtype=float)
        if len(closes) < 10:
            return current_price, None, None
        returns = np.diff(np.log(closes))
        volatility = float(np.std(returns) * np.sqrt(525600))
        volatility = max(0.10, min(volatility, 2.50))
        drift = float(np.mean(returns) * 525600)
        drift = max(-1.0, min(drift, 1.0))
        return current_price, volatility, drift
    except Exception:
        return None, None, None


def _gbm_terminal_prices(s0, time_to_expiry_sec, volatility, drift, simulations, rng=None):
    """Precios terminales S_T bajo GBM (mismo esquema de pasos que el sniper)."""
    if volatility is None or volatility <= 0 or time_to_expiry_sec <= 0:
        return np.full(simulations, s0, dtype=float)
    T = time_to_expiry_sec / (365 * 24 * 60 * 60)
    steps = max(10, int(time_to_expiry_sec / 5))
    steps = min(steps, 240)
    dt = T / steps
    shocks = rng.normal(0, 1, (simulations, steps)) if rng is not None else np.random.normal(0, 1, (simulations, steps))
    paths = np.zeros((simulations, steps + 1), dtype=float)
    paths[:, 0] = s0
    for t in range(1, steps + 1):
        paths[:, t] = paths[:, t - 1] * np.exp(
            (drift - 0.5 * volatility**2) * dt + volatility * np.sqrt(dt) * shocks[:, t - 1]
        )
    return paths[:, -1]


def cortex_mc_prob_yes(s0, strike, time_sec, vol, drift, simulations=2000):
    finals = _gbm_terminal_prices(s0, time_sec, vol, drift, simulations)
    return float(np.mean(finals > strike))


def cortex_stable_prob_yes(s0, strike, time_sec, vol, drift, simulations=2000):
    p_d = cortex_mc_prob_yes(s0, strike, time_sec, vol, drift, simulations)
    p0 = cortex_mc_prob_yes(s0, strike, time_sec, vol, 0.0, simulations)
    return 0.30 * p_d + 0.70 * p0


def calculate_paths(current_price, volatility, drift, time_to_expiry_sec, sims=SIMULACIONES_GRAPH, max_steps=120):
    if time_to_expiry_sec <= 0:
        time_to_expiry_sec = 1
    if volatility is None or volatility <= 0:
        volatility = 0.5
    T = time_to_expiry_sec / (365 * 24 * 60 * 60)
    steps = max(10, int(time_to_expiry_sec / 5))
    steps = min(steps, max_steps)
    dt = T / steps
    random_shocks = np.random.normal(0, 1, (sims, steps))
    paths = np.zeros((sims, steps + 1))
    paths[:, 0] = current_price
    for t in range(1, steps + 1):
        paths[:, t] = paths[:, t - 1] * np.exp(
            (drift - 0.5 * volatility**2) * dt + volatility * np.sqrt(dt) * random_shocks[:, t - 1]
        )
    return paths


def run_sniper_pnl_lab(
    ticker,
    use_live_binance,
    spot_manual,
    strike_manual,
    vol_manual,
    drift_manual,
    time_left_sec,
    ask_yes,
    ask_no,
    trade_usd,
    max_shares,
    min_edge,
    edge_buffer,
    n_prob_sims,
    n_out_sims,
    fee_bps,
):
    """
    Simula una decisión tipo sniper (YES / NO / SKIP) y distribución de PnL por trade
    bajo GBM alineado al motor anti-sesgo (prob estable).
    """
    n_prob_sims = int(max(500, min(n_prob_sims, 8000)))
    n_out_sims = int(max(500, min(n_out_sims, 8000)))
    fee_bps = float(max(0.0, min(fee_bps, 100.0)))

    if use_live_binance and ticker in BINANCE_TICKERS:
        s0, vol, drift = get_binance_data(ticker)
        if not s0:
            return {"error": "No se pudo leer Binance (spot)."}
        if vol is None or vol <= 0:
            return {"error": "Volatilidad no disponible (Binance). Reintenta en un minuto."}
        strike = float(strike_manual) if strike_manual is not None else s0
    else:
        s0 = float(spot_manual)
        vol = float(vol_manual)
        drift = float(drift_manual)
        strike = float(strike_manual) if strike_manual is not None else s0

    if vol <= 0 or time_left_sec <= 0:
        return {"error": "vol y tiempo deben ser > 0."}

    rng = np.random.default_rng()

    p_yes = cortex_stable_prob_yes(s0, strike, float(time_left_sec), vol, drift, simulations=n_prob_sims)
    edge_yes = p_yes - float(ask_yes) - float(edge_buffer)
    edge_no = (1.0 - p_yes) - float(ask_no) - float(edge_buffer)

    side = None
    entry = 0.0
    if edge_yes > float(min_edge) and float(ask_yes) < 0.85:
        side, entry = "YES", float(ask_yes)
    elif edge_no > float(min_edge) and float(ask_no) < 0.85:
        side, entry = "NO", float(ask_no)

    if side is None or entry <= 0:
        return {
            "error": None,
            "skip": True,
            "p_yes": p_yes,
            "edge_yes": edge_yes,
            "edge_no": edge_no,
            "s0": s0,
            "strike": strike,
            "vol": vol,
            "drift": drift,
            "pnls": np.zeros(0),
        }

    shares = min(float(trade_usd) / entry, float(max_shares))
    shares = max(1.0, round(shares, 2))
    notional = shares * entry
    fee = notional * (fee_bps / 10000.0)

    finals = _gbm_terminal_prices(s0, float(time_left_sec), vol, drift, n_out_sims, rng=rng)
    if side == "YES":
        wins = finals > strike
        gross = np.where(wins, shares * 1.0, 0.0)
    else:
        wins = finals < strike
        gross = np.where(wins, shares * 1.0, 0.0)
    pnls = gross - notional - fee

    return {
        "error": None,
        "skip": False,
        "side": side,
        "entry": entry,
        "shares": shares,
        "notional": notional,
        "fee": fee,
        "p_yes": p_yes,
        "edge_yes": edge_yes,
        "edge_no": edge_no,
        "s0": s0,
        "strike": strike,
        "vol": vol,
        "drift": drift,
        "pnls": pnls,
        "wins": wins,
    }

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
    global SIMULATION_DATA, PROB_SPARK
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
                    if not current_px or vol is None:
                        continue
                    
                    price_to_beat = price_to_beat_raw if price_to_beat_raw > 0 else current_px
                    
                    full_paths = calculate_paths(current_px, vol, drift, time_left_sec, sims=2000, max_steps=120)
                    final_prices = full_paths[:, -1]
                    prob_yes = cortex_stable_prob_yes(
                        current_px, price_to_beat, time_left_sec, vol, drift, simulations=1500
                    )
                    
                    ask_no = 0.5
                    try:
                        book_yes = client.get_order_book(tids[0])
                        book_no = client.get_order_book(tids[1])
                        ask_yes = min([float(a.price) for a in book_yes.asks]) if book_yes.asks else 0.99
                        ask_no = min([float(a.price) for a in book_no.asks]) if book_no.asks else 0.99
                    except Exception:
                        book_yes = None
                        ask_yes = 0.5
                        
                    edge = prob_yes - ask_yes
                    impact = calculate_impact(getattr(book_yes, 'asks', []), 25.0) if book_yes and getattr(book_yes, 'asks', None) else 0
                    
                    SIMULATION_DATA.update({
                        "ticker": ticker,
                        "market": f"{mkt.get('question').upper()}",
                        "paths": full_paths[:SIMULACIONES_GRAPH, :],
                        "final_prices": final_prices,
                        "target_price": price_to_beat,
                        "current_price": current_px,
                        "prob_yes": prob_yes,
                        "ask_yes": ask_yes,
                        "ask_no": ask_no,
                        "edge_yes": edge,
                        "price_impact": impact,
                        "time_left": time_left_sec,
                        "market_id": mkt.get("id"),
                    })
                    PROB_SPARK.append(float(prob_yes))
                    PROB_SPARK = PROB_SPARK[-60:]
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


def get_straddle_trades(limit=50):
    """Lee trades_history_straddle.json y devuelve últimos eventos del bot de straddle."""
    try:
        if not os.path.exists(STRADDLE_TRADES_LOG_FILE):
            return []
        with open(STRADDLE_TRADES_LOG_FILE, "r") as f:
            history = json.load(f)
        if not isinstance(history, list):
            return []
        history.sort(key=lambda x: x.get("timestamp") or "", reverse=True)
        return history[:limit]
    except Exception:
        return []


def _render_straddle_trades(trades):
    if not trades:
        return html.Div("Sin eventos aún.", className="tech-gray", style={"fontSize": "10px"})
    rows = []
    for t in trades:
        ts = (t.get("timestamp") or "")[:16].replace("T", " ")
        if ts.endswith("Z"):
            ts = ts[:-1]
        mkt = t.get("market", "—")
        res = t.get("result", "—")
        res_color = "var(--accent-emerald)" if res == "FILLED_2_LEGS" else "var(--accent-red)" if res in ("STOP_1_LEG", "STOP_TIMEOUT") else "var(--text-dim)"

        usd_leg = (t.get("params") or {}).get("usd_per_leg")
        first = t.get("first") or {}
        stop = t.get("stop") or {}
        # PnL aproximado solo para STOP: (sell-buy)*shares (sin fees)
        pnl_est = None
        if res in ("STOP_1_LEG", "STOP_TIMEOUT") and first.get("buy_price") and stop.get("sell_price") and first.get("shares"):
            try:
                pnl_est = (float(stop["sell_price"]) - float(first["buy_price"])) * float(first["shares"])
            except Exception:
                pnl_est = None
        pnl_color = "var(--accent-emerald)" if (pnl_est is not None and pnl_est >= 0) else "var(--accent-red)"
        pnl_str = f"{pnl_est:+.3f}" if pnl_est is not None else "—"

        rows.append(html.Tr([
            html.Td(ts, style={"padding": "2px 6px", "color": "var(--text-dim)"}),
            html.Td(mkt, style={"padding": "2px 6px"}),
            html.Td(res, style={"padding": "2px 6px", "color": res_color, "fontWeight": "600"}),
            html.Td(f"${usd_leg}" if usd_leg is not None else "—", style={"padding": "2px 6px"}),
            html.Td(pnl_str, style={"padding": "2px 6px", "color": pnl_color}),
        ]))
    return html.Table([
        html.Thead(html.Tr([
            html.Th("Hora", style={"padding": "2px 6px", "textAlign": "left", "fontSize": "9px"}),
            html.Th("Mkt", style={"padding": "2px 6px", "fontSize": "9px"}),
            html.Th("Evento", style={"padding": "2px 6px", "fontSize": "9px"}),
            html.Th("$/leg", style={"padding": "2px 6px", "fontSize": "9px"}),
            html.Th("PnL* (stop)", style={"padding": "2px 6px", "fontSize": "9px"}),
        ]), style={"borderBottom": "1px solid var(--border-subtle)"}),
        html.Tbody(rows)
    ], style={"width": "100%", "borderCollapse": "collapse"})


def _render_straddle_summary(trades):
    filled = sum(1 for t in trades if t.get("result") == "FILLED_2_LEGS")
    stops = sum(1 for t in trades if t.get("result") in ("STOP_1_LEG", "STOP_TIMEOUT"))
    total = len(trades)
    return html.Small(f"Eventos: {total} | 2 legs: {filled} | stops: {stops}  (PnL* solo estima stops; 2 legs resuelve después)", className="tech-gray d-block mb-2")


def _parse_float_csv(s: str):
    vals = []
    if not s:
        return vals
    for x in s.split(","):
        x = x.strip()
        if not x:
            continue
        vals.append(float(x))
    return vals


def _load_optimizer_markets_cached():
    global STRADDLE_OPT_CACHE
    if not os.path.exists(STRADDLE_SNAPSHOTS_FILE):
        return None, 0
    mtime = os.path.getmtime(STRADDLE_SNAPSHOTS_FILE)
    if STRADDLE_OPT_CACHE["markets"] is not None and STRADDLE_OPT_CACHE["mtime"] == mtime:
        return STRADDLE_OPT_CACHE["markets"], STRADDLE_OPT_CACHE["lines"]
    markets, n_lines = load_straddle_markets(STRADDLE_SNAPSHOTS_FILE)
    STRADDLE_OPT_CACHE = {"mtime": mtime, "markets": markets, "lines": n_lines}
    return markets, n_lines


def _empty_lab_fig(hint: str):
    fig = go.Figure()
    fig.add_annotation(
        text=hint,
        xref="paper",
        yref="paper",
        x=0.5,
        y=0.5,
        showarrow=False,
        font=dict(color="#6b7280", size=11, family="JetBrains Mono"),
    )
    fig.update_layout(
        paper_bgcolor="#030308",
        plot_bgcolor="#030308",
        margin=dict(l=16, r=16, t=16, b=16),
        xaxis=dict(visible=False),
        yaxis=dict(visible=False),
    )
    return fig


LAB_EMPTY_FIG = _empty_lab_fig("Pulsa «Simular PnL»")


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
            /* Formularios LAB: texto oscuro sobre fondo claro */
            input[type="text"], input[type="number"], input[type="search"], input[type="tel"] {
                color: #0f172a !important;
                background-color: #e8edf4 !important;
                border: 1px solid #64748b !important;
                border-radius: 4px !important;
            }
            input::placeholder { color: #475569 !important; opacity: 1 !important; }
            /* dcc.Dropdown (react-select) */
            .cortex-select .Select-control,
            .cortex-select div[class*="-control"] {
                background-color: #e8edf4 !important;
                border-color: #64748b !important;
                min-height: 34px;
            }
            .cortex-select .Select-value-label,
            .cortex-select .Select-input > input,
            .cortex-select div[class*="singleValue"] {
                color: #0f172a !important;
            }
            .cortex-select .Select-menu-outer,
            .cortex-select div[class*="-menu"] {
                background-color: #f1f5f9 !important;
            }
            .cortex-select .Select-option { color: #0f172a !important; }
            .cortex-select .Select-option.is-focused { background-color: #cbd5e1 !important; }
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
                        html.Span("STRADDLE (OPCIÓN 2)", className="label-small mt-2"),
                        html.Div(id="straddle-summary"),
                        html.Div(id="straddle-trades", style={
                            'fontSize': '10px',
                            'maxHeight': '200px',
                            'overflowY': 'auto',
                            'border': '1px solid var(--border-subtle)',
                            'borderRadius': '6px',
                            'padding': '8px',
                            'marginBottom': '12px'
                        }),
                    ]),
                    html.Div([
                        html.Span("LAB: OPTIMIZADOR STRADDLE", className="label-small mt-2"),
                        html.Small("Busca mejores parámetros con grid search", className="tech-gray d-block mb-2"),
                        html.Div([
                            html.Small("timeout_sec (csv)", className="tech-gray"),
                            dcc.Input(id="opt-timeout-values", type="text", value="20,30,45,60,90", style=_field(width="100%", marginBottom="6px")),
                            html.Small("usd_per_leg (csv)", className="tech-gray"),
                            dcc.Input(id="opt-usd-values", type="text", value="1,2,3,4", style=_field(width="100%", marginBottom="6px")),
                            html.Small("limit_price (csv)", className="tech-gray"),
                            dcc.Input(id="opt-limit-values", type="text", value="0.30,0.32,0.35", style=_field(width="100%", marginBottom="6px")),
                            html.Small("other_within (csv)", className="tech-gray"),
                            dcc.Input(id="opt-other-values", type="text", value="0.01,0.02,0.03", style=_field(width="100%", marginBottom="6px")),
                            html.Small("confirm_sec (csv)", className="tech-gray"),
                            dcc.Input(id="opt-confirm-values", type="text", value="30,45,60,90", style=_field(width="100%", marginBottom="6px")),
                            html.Small("min trades", className="tech-gray"),
                            dcc.Input(id="opt-min-trades", type="number", value=30, min=1, step=1, style=_field(width="100%", marginBottom="6px")),
                            html.Small("max drawdown (negativo = sin filtro)", className="tech-gray"),
                            dcc.Input(id="opt-max-dd", type="number", value=-1, step=1, style=_field(width="100%", marginBottom="8px")),
                            html.Button("Buscar mejor combinación", id="run-straddle-opt-btn", n_clicks=0, style={"width": "100%", "fontSize": "11px"}),
                        ], style={"border": "1px solid var(--border-subtle)", "padding": "8px", "borderRadius": "6px"}),
                        html.Div(id="straddle-opt-results", style={"marginTop": "8px", "fontSize": "10px"}),
                        html.Small("Top combinaciones (PnL simulado)", className="tech-gray d-block mt-2 mb-1"),
                        dcc.Graph(id="straddle-opt-chart", figure=LAB_EMPTY_FIG, config={"displayModeBar": False}, style={"height": "200px"}),
                    ], style={"marginBottom": "12px"}),
                    html.Div([
                        html.Span("LAB: SIMULADOR SNIPER (PnL teórico)", className="label-small mt-2"),
                        html.Small(
                            "Misma regla que el bot: prob estable + edge mínimo + buffer. Tras simular verás veredicto ROJO/AMBAR/VERDE según EV, "
                            "% de escenarios perdedores y línea de EV en el histograma — para no confundir “operar mucho” con “acercarse a 1M”.",
                            className="tech-gray d-block mb-2",
                        ),
                        html.Small(
                            "No necesitas escribir el price to beat a mano para cada mercado de 5m: el motor ya lo toma de Polymarket/Binance. "
                            "Usa el botón de abajo para copiar esa señal al formulario, o el optimizador straddle para backtest masivo desde snapshots.",
                            className="tech-gray d-block mb-2",
                        ),
                        html.Button(
                            "Cargar mercado actual (motor en vivo + CLOB)",
                            id="lab-fill-from-live-btn",
                            n_clicks=0,
                            style={"width": "100%", "fontSize": "10px", "marginBottom": "6px"},
                        ),
                        html.Button(
                            "Cargar último snapshot (JSONL + Gamma/Binance)",
                            id="lab-fill-from-snapshot-btn",
                            n_clicks=0,
                            style={"width": "100%", "fontSize": "10px", "marginBottom": "6px"},
                        ),
                        html.Div(id="lab-live-fill-msg", style={"fontSize": "10px", "marginBottom": "8px"}),
                        dcc.Dropdown(
                            id="lab-ticker",
                            className="cortex-select",
                            options=[{"label": "BTC", "value": "BTC"}, {"label": "ETH", "value": "ETH"}],
                            value="BTC",
                            clearable=False,
                            style=_field(width="100%", marginBottom="6px", fontSize="11px"),
                        ),
                        dcc.Checklist(
                            id="lab-use-live",
                            options=[{"label": " Usar Binance live (spot, vol, drift)", "value": "live"}],
                            value=["live"],
                            style={"fontSize": "10px", "marginBottom": "6px"},
                            labelStyle={"color": "#cbd5e1", "fontWeight": "500"},
                            inputStyle={"marginRight": "8px"},
                        ),
                        html.Small("Manual (si quitas live): spot, vol anualizada, drift anualizado, strike", className="tech-gray d-block"),
                        dcc.Input(id="lab-spot", type="number", placeholder="spot", style=_field(width="48%", marginRight="4%", marginBottom="6px")),
                        dcc.Input(id="lab-strike", type="number", placeholder="strike / priceToBeat", style=_field(width="48%", marginBottom="6px")),
                        dcc.Input(id="lab-vol", type="number", placeholder="vol (ej 0.6)", value=0.6, step=0.05, style=_field(width="48%", marginRight="4%", marginBottom="6px")),
                        dcc.Input(id="lab-drift", type="number", placeholder="drift (ej 0)", value=0.0, step=0.05, style=_field(width="48%", marginBottom="6px")),
                        html.Small("Mercado (asks) y tamaño", className="tech-gray d-block mt-1"),
                        dcc.Input(id="lab-time-left", type="number", value=300, min=30, step=30, style=_field(width="32%", marginRight="2%", marginBottom="6px")),
                        dcc.Input(id="lab-ask-yes", type="number", value=0.35, min=0.01, max=0.99, step=0.01, style=_field(width="32%", marginRight="2%", marginBottom="6px")),
                        dcc.Input(id="lab-ask-no", type="number", value=0.62, min=0.01, max=0.99, step=0.01, style=_field(width="32%", marginBottom="6px")),
                        dcc.Input(id="lab-trade-usd", type="number", value=2.0, min=0.5, step=0.5, style=_field(width="48%", marginRight="4%", marginBottom="6px")),
                        dcc.Input(id="lab-max-shares", type="number", value=4.0, min=1, step=0.5, style=_field(width="48%", marginBottom="6px")),
                        dcc.Input(id="lab-min-edge", type="number", value=0.10, min=0, max=0.5, step=0.01, style=_field(width="48%", marginRight="4%", marginBottom="6px")),
                        dcc.Input(id="lab-edge-buffer", type="number", value=0.03, min=0, max=0.2, step=0.01, style=_field(width="48%", marginBottom="6px")),
                        dcc.Input(id="lab-n-prob", type="number", value=2500, min=500, max=8000, step=100, style=_field(width="48%", marginRight="4%", marginBottom="6px")),
                        dcc.Input(id="lab-n-out", type="number", value=2500, min=500, max=8000, step=100, style=_field(width="48%", marginBottom="6px")),
                        dcc.Input(id="lab-fee-bps", type="number", value=25, min=0, max=100, step=1, style=_field(width="100%", marginBottom="8px")),
                        html.Button("Simular PnL", id="lab-sniper-run-btn", n_clicks=0, style={"width": "100%", "fontSize": "11px", "marginBottom": "8px"}),
                        html.Div(id="lab-sniper-summary", style={"fontSize": "10px", "marginBottom": "8px"}),
                        html.Small("Distribución PnL (USDC)", className="tech-gray d-block mb-1"),
                        dcc.Graph(id="lab-sniper-pnl-hist", figure=LAB_EMPTY_FIG, config={"displayModeBar": False}, style={"height": "200px"}),
                        html.Small("Equity acumulada (trades i.i.d. ilustrativos)", className="tech-gray d-block mb-1"),
                        dcc.Graph(id="lab-sniper-equity", figure=LAB_EMPTY_FIG, config={"displayModeBar": False}, style={"height": "180px"}),
                    ], style={"marginBottom": "12px", "border": "1px solid var(--border-subtle)", "padding": "8px", "borderRadius": "6px"}),
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
     Output("closed-trades", "children"),
     Output("straddle-summary", "children"),
     Output("straddle-trades", "children")],
    [Input('interval-update', 'n_intervals')]
)
def update_cortex(n):
    global PROB_SPARK
    data = SIMULATION_DATA

    # Sparkline: últimos prob YES del motor (en %); fallback suave si aún no hay datos
    if len(PROB_SPARK) >= 3:
        spark_y = np.array(PROB_SPARK, dtype=float) * 100.0
    else:
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
        st = get_straddle_trades()
        st_sum = _render_straddle_summary(st)
        st_ui = _render_straddle_trades(st)
        return idle_fig, idle_fig, spark, "Waiting for market signal...", "—", "—", "—", logs, f"${data['current_balance']:,.2f}", f"Session PnL: ${data['total_pnl']:+.2f}", closed_ui, st_sum, st_ui

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
    st = get_straddle_trades()
    st_sum = _render_straddle_summary(st)
    st_ui = _render_straddle_trades(st)

    return (
        fig, hist, spark,
        f"{data['ticker']} · {data['market'][:50]}{'...' if len(data['market']) > 50 else ''}",
        f"{data['prob_yes']:.1%}",
        f"{data['price_impact']:.2%}",
        time_str,
        logs,
        f"${data['current_balance']:,.2f}",
        f"Session PnL: ${data['total_pnl']:+.2f}",
        closed_ui,
        st_sum,
        st_ui
    )


@app.callback(
    [Output("straddle-opt-results", "children"), Output("straddle-opt-chart", "figure")],
    Input("run-straddle-opt-btn", "n_clicks"),
    State("opt-timeout-values", "value"),
    State("opt-usd-values", "value"),
    State("opt-limit-values", "value"),
    State("opt-other-values", "value"),
    State("opt-confirm-values", "value"),
    State("opt-min-trades", "value"),
    State("opt-max-dd", "value"),
)
def run_straddle_optimizer_ui(
    n_clicks,
    timeout_values_s,
    usd_values_s,
    limit_values_s,
    other_values_s,
    confirm_values_s,
    min_trades,
    max_dd,
):
    if not n_clicks:
        return (
            html.Small("Configura rangos y presiona 'Buscar mejor combinación'.", className="tech-gray"),
            LAB_EMPTY_FIG,
        )

    try:
        timeout_values = _parse_float_csv(timeout_values_s or "")
        usd_values = _parse_float_csv(usd_values_s or "")
        limit_values = _parse_float_csv(limit_values_s or "")
        other_values = _parse_float_csv(other_values_s or "")
        confirm_values = _parse_float_csv(confirm_values_s or "")
        min_trades = int(min_trades or 30)
        max_dd_val = float(max_dd) if max_dd is not None else -1.0
        max_dd_limit = None if max_dd_val < 0 else max_dd_val

        if not (timeout_values and usd_values and limit_values and other_values and confirm_values):
            return (
                html.Small("Error: todos los rangos deben tener al menos un valor.", className="tech-red"),
                LAB_EMPTY_FIG,
            )

        markets, n_lines = _load_optimizer_markets_cached()
        if not markets:
            return (
                html.Small(f"No hay snapshots en {STRADDLE_SNAPSHOTS_FILE}", className="tech-red"),
                LAB_EMPTY_FIG,
            )

        out = run_straddle_grid(
            markets=markets,
            timeout_values=timeout_values,
            usd_values=usd_values,
            limit_values=limit_values,
            other_within_values=other_values,
            confirm_values=confirm_values,
            min_trades=min_trades,
            max_drawdown_limit=max_dd_limit,
            top_k=20,
            max_shares_per_leg=6.0,
        )

        top = out.get("top", [])
        if not top:
            fig0 = _empty_lab_fig("Sin resultados con estos filtros")
            return (
                html.Div(
                    [
                        html.Small(
                            f"Sin resultados con los filtros actuales. tested={out.get('tested', 0)} kept=0",
                            className="tech-red",
                        )
                    ]
                ),
                fig0,
            )

        cols = [
            "timeout_sec", "usd_per_leg", "limit_price", "other_within", "confirm_sec",
            "trades", "conv_2legs", "stops", "pnl_total", "pnl_btc", "pnl_eth", "max_drawdown", "score"
        ]
        rows = []
        for r in top:
            row = {k: r.get(k) for k in cols}
            for k in ("pnl_total", "pnl_btc", "pnl_eth", "max_drawdown", "score"):
                if isinstance(row.get(k), (int, float)):
                    row[k] = round(float(row[k]), 3)
            rows.append(row)

        info = html.Small(
            f"Snapshots: {n_lines:,} | Markets: {len(markets):,} | Tested: {out.get('tested', 0):,} | Kept: {out.get('kept', 0):,}",
            className="tech-gray d-block mb-2",
        )
        table = dash_table.DataTable(
            data=rows,
            columns=[{"name": c, "id": c} for c in cols],
            page_size=20,
            style_table={"overflowX": "auto"},
            style_cell={"fontSize": "10px", "backgroundColor": "#070712", "color": "#cbd5e1", "border": "1px solid #1f2937"},
            style_header={"backgroundColor": "#0b1220", "color": "#7dd3fc", "fontWeight": "bold"},
        )

        bar_rows = top[:10]
        labels = [
            f"t{float(r.get('timeout_sec', 0)):.0f}_u{float(r.get('usd_per_leg', 0)):.1f}_L{float(r.get('limit_price', 0)):.2f}"
            for r in bar_rows
        ]
        pnls_bar = [float(r.get("pnl_total", 0) or 0) for r in bar_rows]
        colors = ["#00ff88" if p >= 0 else "#ff3366" for p in pnls_bar]
        fig_bar = go.Figure(
            go.Bar(
                x=labels,
                y=pnls_bar,
                marker_color=colors,
                marker_line_width=0,
                hovertemplate="PnL: %{y:.2f}<extra></extra>",
            )
        )
        fig_bar.update_layout(
            paper_bgcolor="#030308",
            plot_bgcolor="#030308",
            margin=dict(l=40, r=12, t=28, b=72),
            title=dict(text="Top 10 · PnL total simulado (USDC)", font=dict(size=10, color="#6b7280")),
            xaxis=dict(tickangle=-35, color="#6b7280", showgrid=False),
            yaxis=dict(color="#6b7280", gridcolor="rgba(0,212,255,0.08)", title="PnL"),
            showlegend=False,
        )
        fig_bar.add_hline(y=0, line_dash="dash", line_color="#6b7280", line_width=1)

        return html.Div([info, table]), fig_bar
    except Exception as e:
        return html.Small(f"Error optimizador: {e}", className="tech-red"), LAB_EMPTY_FIG


def _lab_parse_float(x, default=None):
    if x is None or x == "":
        return default
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _read_last_orderbook_snapshot(path: str):
    """Última línea válida de orderbook_snapshots.jsonl (orderbook_recorder)."""
    path = os.path.expanduser(path)
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            if size == 0:
                return None
            chunk = min(size, 65536)
            f.seek(max(0, size - chunk))
            raw = f.read().decode("utf-8", errors="ignore")
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        for ln in reversed(lines):
            try:
                return json.loads(ln)
            except json.JSONDecodeError:
                continue
    except OSError:
        return None
    return None


def _gamma_market_event_start(market_id):
    if not market_id:
        return None
    try:
        r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=8)
        if r.status_code != 200:
            return None
        m = r.json()
        if not isinstance(m, dict):
            return None
        return m.get("eventStartTime") or m.get("startDate")
    except Exception:
        return None


@app.callback(
    [
        Output("lab-spot", "value"),
        Output("lab-strike", "value"),
        Output("lab-time-left", "value"),
        Output("lab-ask-yes", "value"),
        Output("lab-ask-no", "value"),
        Output("lab-ticker", "value"),
        Output("lab-live-fill-msg", "children"),
    ],
    [
        Input("lab-fill-from-live-btn", "n_clicks"),
        Input("lab-fill-from-snapshot-btn", "n_clicks"),
    ],
    prevent_initial_call=True,
)
def fill_lab_from_sources(_n_live, _n_snap):
    if not callback_context.triggered:
        raise PreventUpdate
    tid = callback_context.triggered[0]["prop_id"].split(".")[0]

    if tid == "lab-fill-from-live-btn":
        data = SIMULATION_DATA
        if not data.get("paths") or len(data.get("paths", [])) == 0:
            return (
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                html.Small(
                    "Aún no hay mercado en el motor central (sin gráfica). Espera unos segundos o revisa que haya mercados 5m BTC/ETH.",
                    className="tech-red",
                ),
            )
        tl = int(max(30, min(1200, int(float(data.get("time_left", 300))))))
        tk = data.get("ticker")
        if tk not in ("BTC", "ETH"):
            tk = "BTC"
        return (
            float(data["current_price"]),
            float(data["target_price"]),
            tl,
            float(data["ask_yes"]),
            float(data.get("ask_no", 0.5)),
            tk,
            html.Small(
                f"En vivo: {str(data.get('market', ''))[:40]}… | mkt_id={data.get('market_id')}",
                className="tech-cyan",
            ),
        )

    if tid == "lab-fill-from-snapshot-btn":
        row = _read_last_orderbook_snapshot(STRADDLE_SNAPSHOTS_FILE)
        if not row:
            return (
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                no_update,
                html.Small(
                    f"No hay snapshots en {STRADDLE_SNAPSHOTS_FILE}. Activa orderbook_recorder en la instancia.",
                    className="tech-red",
                ),
            )
        ticker = row.get("ticker") or "BTC"
        if ticker not in ("BTC", "ETH"):
            ticker = "BTC"
        try:
            tl = int(max(30, min(1200, float(row.get("time_left_s") or 300))))
        except (TypeError, ValueError):
            tl = 300
        ask_y = row.get("yes_ask")
        ask_n = row.get("no_ask")
        try:
            ask_y = float(ask_y) if ask_y is not None else 0.5
        except (TypeError, ValueError):
            ask_y = 0.5
        try:
            ask_n = float(ask_n) if ask_n is not None else 0.5
        except (TypeError, ValueError):
            ask_n = 0.5
        mkt_id = row.get("market_id")
        start_s = _gamma_market_event_start(mkt_id)
        s0, _vol, _dr = get_binance_data(ticker)
        if not s0:
            return (
                no_update,
                no_update,
                tl,
                ask_y,
                ask_n,
                ticker,
                html.Small("No se pudo leer spot en Binance.", className="tech-red"),
            )
        strike = 0.0
        if start_s:
            strike = float(get_binance_historical(ticker, start_s))
        if not strike or strike <= 0:
            strike = float(s0)
        st_short = (start_s or "").replace("T", " ")[:19]
        return (
            float(s0),
            float(strike),
            tl,
            ask_y,
            ask_n,
            ticker,
            html.Small(
                f"Última línea JSONL: asks del recorder; strike ≈ apertura Binance en eventStart ({st_short}). mkt_id={mkt_id}",
                className="tech-cyan",
            ),
        )

    raise PreventUpdate


@app.callback(
    [
        Output("lab-sniper-summary", "children"),
        Output("lab-sniper-pnl-hist", "figure"),
        Output("lab-sniper-equity", "figure"),
    ],
    Input("lab-sniper-run-btn", "n_clicks"),
    State("lab-ticker", "value"),
    State("lab-use-live", "value"),
    State("lab-spot", "value"),
    State("lab-strike", "value"),
    State("lab-vol", "value"),
    State("lab-drift", "value"),
    State("lab-time-left", "value"),
    State("lab-ask-yes", "value"),
    State("lab-ask-no", "value"),
    State("lab-trade-usd", "value"),
    State("lab-max-shares", "value"),
    State("lab-min-edge", "value"),
    State("lab-edge-buffer", "value"),
    State("lab-n-prob", "value"),
    State("lab-n-out", "value"),
    State("lab-fee-bps", "value"),
    prevent_initial_call=True,
)
def run_lab_sniper_pnl_ui(
    n_clicks,
    ticker,
    use_live_vals,
    spot_v,
    strike_v,
    vol_v,
    drift_v,
    time_left_v,
    ask_yes_v,
    ask_no_v,
    trade_usd_v,
    max_sh_v,
    min_edge_v,
    edge_buf_v,
    n_prob_v,
    n_out_v,
    fee_bps_v,
):
    if not n_clicks:
        raise PreventUpdate

    use_live = bool(use_live_vals and "live" in use_live_vals)
    strike_opt = _lab_parse_float(strike_v, None)
    if not use_live:
        sp0 = _lab_parse_float(spot_v, None)
        if sp0 is None or sp0 <= 0:
            return (
                html.Small("Modo manual: indica spot > 0.", className="tech-red"),
                LAB_EMPTY_FIG,
                LAB_EMPTY_FIG,
            )
    out = run_sniper_pnl_lab(
        ticker or "BTC",
        use_live,
        _lab_parse_float(spot_v, 0.0) or 0.0,
        strike_opt,
        _lab_parse_float(vol_v, 0.6),
        _lab_parse_float(drift_v, 0.0),
        int(_lab_parse_float(time_left_v, 300) or 300),
        _lab_parse_float(ask_yes_v, 0.35),
        _lab_parse_float(ask_no_v, 0.62),
        _lab_parse_float(trade_usd_v, 2.0),
        _lab_parse_float(max_sh_v, 4.0),
        _lab_parse_float(min_edge_v, 0.10),
        _lab_parse_float(edge_buf_v, 0.03),
        int(_lab_parse_float(n_prob_v, 2500) or 2500),
        int(_lab_parse_float(n_out_v, 2500) or 2500),
        _lab_parse_float(fee_bps_v, 25.0),
    )

    if out.get("error"):
        msg = html.Div(html.Small(out["error"], className="tech-red"))
        return msg, LAB_EMPTY_FIG, LAB_EMPTY_FIG

    if out.get("skip"):
        summ = html.Div(
            [
                html.Small("Sin trade (ningún lado cumple edge mínimo + buffer).", style={"color": "#fbbf24"}),
                html.Br(),
                html.Small(
                    f"Spot≈{out['s0']:.2f} | strike={out['strike']:.2f} | vol={out['vol']:.3f} | drift={out['drift']:.3f}",
                    className="tech-gray",
                ),
                html.Br(),
                html.Small(
                    f"P(YES)≈{out['p_yes']:.1%} | edge YES (tras buffer)={out['edge_yes']:+.1%} | edge NO={out['edge_no']:+.1%}",
                    className="tech-gray",
                ),
                html.Br(),
                html.Small(
                    "Veredicto: el bot NO dispararía aquí. Eso es filtro, no “pérdida de oportunidad”: evita operar sin ventaja clara. "
                    "Hacia 1M importa repetir solo setups con EV+ demostrable, no el conteo de órdenes.",
                    style={"color": "#94a3b8", "fontSize": "10px", "lineHeight": "1.45"},
                ),
            ]
        )
        return summ, LAB_EMPTY_FIG, LAB_EMPTY_FIG

    pnls = out["pnls"]
    ev = float(np.mean(pnls))
    med = float(np.median(pnls))
    wr = float(np.mean(out["wins"]))
    neg_frac = float(np.mean(pnls < 0))
    std_p = float(np.std(pnls))
    p5, p95 = float(np.percentile(pnls, 5)), float(np.percentile(pnls, 95))
    notional = float(out.get("notional") or 0.001)
    # Edge “modelo vs precio” del lado elegido (sin buffer; ya está implícito en la decisión)
    if out["side"] == "YES":
        edge_model = float(out["p_yes"]) - float(out["entry"])
        fair_hint = f"Para YES, precio del mercado pide ~{out['entry']:.1%} de prob implícita; el modelo da ~{out['p_yes']:.1%}."
    else:
        edge_model = (1.0 - float(out["p_yes"])) - float(out["entry"])
        fair_hint = f"Para NO, implícito ~{out['entry']:.1%}; modelo ~{(1.0 - float(out['p_yes'])):.1%}."
    # Veredicto EV (solo esta simulación, i.i.d.)
    if ev < -0.05:
        verdict = ("ROJO", "EV simulado claramente negativo: este punto NO apoya acumulación.", "#ff3366")
    elif ev < 0:
        verdict = ("AMBAR", "EV simulado ligeramente negativo: cautela.", "#fbbf24")
    elif ev < max(0.05, 0.02 * notional):
        verdict = ("AMBAR", "EV positivo pero fino: varianza y fees pueden comerlo en la práctica.", "#fbbf24")
    else:
        verdict = ("VERDE", "EV simulado materialmente positivo (sigue validando con histórico y tamaño de apuesta).", "#00ff88")

    summ = html.Div(
        [
            html.Div(
                f"{verdict[0]} · {verdict[1]}",
                style={"color": verdict[2], "fontWeight": "700", "fontSize": "11px", "marginBottom": "6px"},
            ),
            html.Small(
                f"Lado: {out['side']} @ {out['entry']:.3f} | shares={out['shares']:.2f} | notional≈${out['notional']:.2f} | fee≈${out['fee']:.3f}",
                className="tech-cyan",
            ),
            html.Br(),
            html.Small(
                f"P(YES)≈{out['p_yes']:.1%} | edge modelo vs precio (lado): {edge_model:+.1%} | {fair_hint}",
                className="tech-gray",
            ),
            html.Br(),
            html.Small(
                f"EV/trade≈${ev:+.3f} | mediana≈${med:+.3f} | σ≈${std_p:.3f} | win≈{wr:.1%} | escenarios con pérdida≈{neg_frac:.1%}",
                className="tech-gray",
            ),
            html.Br(),
            html.Small(
                f"p5 / p95 PnL: ${p5:.2f} … ${p95:.2f}",
                className="tech-gray",
            ),
            html.Br(),
            html.Small(
                "Objetivo 1M: ninguna simulación “prueba” el millón; solo mide si ESTE setup (con estos asks/tiempo/modelo) "
                "tiene ventaja esperada positiva en el toy model. Hace falta EV+ sostenido en el tiempo, control de ruina y "
                "reinversión — y validar con trades reales resueltos, no solo tablas.",
                style={"color": "#64748b", "fontSize": "9px", "lineHeight": "1.45"},
            ),
        ]
    )

    fig_h = go.Figure(
        go.Histogram(
            x=pnls,
            nbinsx=36,
            marker_color="rgba(0,212,255,0.55)",
            marker_line_width=0,
        )
    )
    fig_h.add_vline(x=0, line_dash="dash", line_color="#ff3366", line_width=2)
    fig_h.add_vline(x=ev, line_dash="dot", line_color="#fbbf24", line_width=2, annotation_text="EV", annotation_position="top")
    fig_h.update_layout(
        paper_bgcolor="#030308",
        plot_bgcolor="#030308",
        margin=dict(l=40, r=12, t=36, b=36),
        title=dict(
            text=f"PnL por escenario · EV=${ev:.3f} · {neg_frac:.0%} bajo cero",
            font=dict(size=10, color="#94a3b8"),
        ),
        xaxis=dict(color="#6b7280", title="PnL USDC"),
        yaxis=dict(color="#6b7280", title="Frecuencia", gridcolor="rgba(0,212,255,0.06)"),
        showlegend=False,
    )

    eq = np.cumsum(pnls)
    total_eq = float(eq[-1]) if len(eq) else 0.0
    fig_e = go.Figure(
        go.Scatter(
            y=eq,
            mode="lines",
            line=dict(color="#00ff88", width=2),
            fill="tozeroy",
            fillcolor="rgba(0,255,136,0.12)",
        )
    )
    fig_e.add_hline(y=0, line_dash="dot", line_color="#6b7280", line_width=1)
    fig_e.update_layout(
        paper_bgcolor="#030308",
        plot_bgcolor="#030308",
        margin=dict(l=40, r=12, t=40, b=32),
        title=dict(
            text=f"Σ PnL acumulado en {len(eq)} escenarios i.i.d. = ${total_eq:+.2f} (ilustrativo, no es tu cuenta real)",
            font=dict(size=10, color="#94a3b8"),
        ),
        xaxis=dict(color="#6b7280", title="Escenario #"),
        yaxis=dict(color="#6b7280", title="Σ PnL (ilustrativo)", gridcolor="rgba(0,212,255,0.06)"),
        showlegend=False,
    )

    return summ, fig_h, fig_e

if __name__ == "__main__":
    t = threading.Thread(target=neural_engine_loop, daemon=True)
    t.start()
    app.run(debug=False, host="0.0.0.0", port=8050)
