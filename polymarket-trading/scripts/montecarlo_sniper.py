import os
import sys
import time
import json
import logging
import math
import numpy as np
import requests
import fcntl
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds
from web3 import Web3

# ═══════════════════════════════════════════════════════════════════
TRADES_LOG_FILE = os.path.expanduser("~/trades_history.json")
TRADED_MARKETS_FILE = os.path.expanduser("~/.openclaw/workspace/skills/polymarket/traded_markets.json")
LOCK_FILE = os.path.expanduser("~/.openclaw/workspace/skills/polymarket/.sniper.lock")

def log_trade_to_file(trade_data):
    trade_data["resolved"] = False
    try:
        history = []
        if os.path.exists(TRADES_LOG_FILE):
            with open(TRADES_LOG_FILE, "r") as f:
                history = json.load(f)
        history.append(trade_data)
        with open(TRADES_LOG_FILE, "w") as f:
            json.dump(history, f, indent=4)
    except Exception as e:
        log.error(f"Error guardando auditoría de trade: {e}")


def update_trades_resolutions():
    """Actualiza el historial con el resultado (ganado/perdido) de mercados ya resueltos."""
    if not os.path.exists(TRADES_LOG_FILE):
        return
    try:
        with open(TRADES_LOG_FILE, "r") as f:
            history = json.load(f)
        unresolved = [i for i, t in enumerate(history) if not t.get("resolved") and t.get("market_id")]
        if not unresolved:
            return
        updated = 0
        for i in unresolved:
            t = history[i]
            mkt_id = t.get("market_id")
            if not mkt_id:
                continue
            try:
                r = requests.get(f"{GAMMA_API}/markets/{mkt_id}", timeout=5)
                if r.status_code != 200:
                    continue
                m = r.json()
                if not m.get("closed"):
                    continue
                prices = json.loads(m.get("outcomePrices", "[]"))
                if len(prices) < 2:
                    continue
                yes_won = prices[0] == "1" or float(prices[0]) > 0.99
                our_side = t.get("side", "")
                won = (our_side == "YES" and yes_won) or (our_side == "NO" and not yes_won)
                history[i]["resolved"] = True
                history[i]["won"] = won
                history[i]["resolved_at"] = datetime.now(timezone.utc).isoformat()
                history[i]["outcome_yes_won"] = yes_won
                inv = float(t.get("investment", 0) or 0)
                px = float(t.get("price", 1) or 1)
                if won:
                    history[i]["pnl"] = round(inv * (1.0 / px - 1.0), 2)
                else:
                    history[i]["pnl"] = round(-inv, 2)
                updated += 1
            except Exception:
                continue
        if updated:
            with open(TRADES_LOG_FILE, "w") as f:
                json.dump(history, f, indent=4)
            log.info(f"📊 Resoluciones actualizadas: {updated} mercados (ganado/perdido en historial)")
    except Exception as e:
        log.debug(f"update_trades_resolutions: {e}")

LAST_REDEEM_TIME = 0

def auto_redeem_if_needed(client):
    # Temporalmente desactivado mientras verificamos la función correcta en la librería
    pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("MonteCarloSniper")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

# Telegram Config (envía a ti: usa TELEGRAM_CHAT_ID o, si no existe, el primer ID de TELEGRAM_GROUP_IDS)
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or (os.getenv("TELEGRAM_GROUP_IDS") or "").strip().split(",")[0].strip() or None

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": f"🛡️ SNIPER: {msg}"}, timeout=5)
    except Exception:
        pass

GAMMA_API = "https://gamma-api.polymarket.com"
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")

# Parámetros conservadores
# Tamaño fijo: el usuario pidió NO escalar por saldo; mantener $2 por operación.
FIXED_TRADE_USD = 2.00
FIXED_MAX_SHARES = 4.0  # ~2 shares por USD de tamaño
MAX_RISK_PER_TRADE = 0.02 # Máximo 2% del bankroll
MIN_EDGE_REQUIRED = 0.10  # 10% edge mínimo
EDGE_BUFFER = 0.03        # Colchón por fricción/modelo: exige +3pp extra de edge
MIN_MARKET_AGE_SEC = 120  # Evita entrar en los primeros 2 minutos del mercado
SIMULACIONES = 10000      
TRADED_MARKETS = set()    # MEMORIA: No repetir mercados (también persistido en disco)


def get_trade_size_usd(balance: float) -> tuple[float, float]:
    """
    Tamaño fijo por operación (sin escala por saldo).
    Devuelve (trade_usd, max_shares) para usar en la orden.
    """
    return (FIXED_TRADE_USD, FIXED_MAX_SHARES)

BINANCE_TICKERS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def load_traded_markets():
    """Carga mercados ya operados desde disco (evita doble orden tras reinicio)."""
    global TRADED_MARKETS
    try:
        if os.path.exists(TRADED_MARKETS_FILE):
            with open(TRADED_MARKETS_FILE, "r") as f:
                data = json.load(f)
                TRADED_MARKETS = set(data.get("market_ids", []))
                if TRADED_MARKETS:
                    log.info(f"Cargados {len(TRADED_MARKETS)} mercados ya operados (evitar doble orden)")
    except Exception as e:
        log.warning(f"No se pudo cargar traded_markets: {e}")


def save_traded_markets():
    """Persiste TRADED_MARKETS a disco tras cada operación."""
    try:
        dirname = os.path.dirname(TRADED_MARKETS_FILE)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname, exist_ok=True)
        with open(TRADED_MARKETS_FILE, "w") as f:
            json.dump({"market_ids": list(TRADED_MARKETS), "updated": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    except Exception as e:
        log.error(f"Error guardando traded_markets: {e}")


def acquire_lock():
    """Lock file para una sola instancia. Si ya hay otra corriendo, sale."""
    try:
        dirname = os.path.dirname(LOCK_FILE)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname, exist_ok=True)
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Escribir PID para diagnóstico
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except (OSError, BlockingIOError):
        log.error("Otra instancia del sniper ya está corriendo (lock activo). Salida.")
        sys.exit(1)

def get_clob_client():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    creds = ApiCreds(
        os.getenv("POLYMARKET_API_KEY"),
        os.getenv("POLYMARKET_API_SECRET"),
        os.getenv("POLYMARKET_API_PASSPHRASE")
    )
    proxy = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")
    return ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=2, funder=proxy)

# ── Binance Data ──────────────────────────────────────────────────
def get_binance_historical(ticker, iso_str):
    """Precio de Binance al inicio del intervalo (para usar como target cuando la API no da priceToBeat)."""
    try:
        ts = int(datetime.fromisoformat(iso_str.replace("Z", "+00:00")).timestamp() * 1000)
        symbol = BINANCE_TICKERS.get(ticker)
        if not symbol:
            return 0.0
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&startTime={ts}&limit=1"
        r = requests.get(url, timeout=3).json()
        if r and len(r) > 0:
            return float(r[0][1])  # open del primer minuto
        return 0.0
    except Exception:
        return 0.0


def get_binance_data(ticker):
    """Obtiene precio actual y calcula volatilidad del último par de horas."""
    symbol = BINANCE_TICKERS.get(ticker)
    if not symbol: return None, None
    
    try:
        # Precio actual
        r_px = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}", timeout=3).json()
        current_price = float(r_px["price"])
        
        # Velas de 1 minuto para calcular volatilidad (últimas 60 velas)
        r_kl = requests.get(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&limit=60", timeout=3).json()
        
        closes = np.array([float(k[4]) for k in r_kl], dtype=float)
        if len(closes) < 10:
            return current_price, None, None
        returns = np.diff(np.log(closes))  # Log-returns por minuto (más estables)

        # Volatilidad anualizada con piso/techo para evitar extremos de ruido.
        volatility = float(np.std(returns) * np.sqrt(525600))
        volatility = max(0.10, min(volatility, 2.50))

        # Drift anualizado acotado para reducir sesgo por momentum de muy corto plazo.
        drift = float(np.mean(returns) * 525600)
        drift = max(-1.0, min(drift, 1.0))
        
        return current_price, volatility, drift
    except Exception as e:
        log.error(f"Error Binance {ticker}: {e}")
        return None, None, None

# ── Monte Carlo GBM ───────────────────────────────────────────────
def monte_carlo_probability(current_price, target_price, time_to_expiry_sec, volatility, drift, simulations=SIMULACIONES):
    """
    Simula múltiples escenarios futuros de precio para encontrar la probabilidad real.
    """
    if time_to_expiry_sec <= 0:
        return 1.0 if current_price > target_price else 0.0

    T = time_to_expiry_sec / (365 * 24 * 60 * 60) # Tiempo en años
    steps = max(10, int(time_to_expiry_sec / 5))  # Pasos cada 5 segundos
    dt = T / steps
    
    # Matriz de simulaciones: shape (simulations, steps)
    random_shocks = np.random.normal(0, 1, (simulations, steps))
    
    # Camino simulado (Geometric Brownian Motion)
    # S_t = S_0 * exp((mu - 0.5 * sigma^2)*t + sigma * W_t)
    paths = np.zeros((simulations, steps + 1))
    paths[:, 0] = current_price
    
    for t in range(1, steps + 1):
        paths[:, t] = paths[:, t-1] * np.exp((drift - 0.5 * volatility**2) * dt + volatility * np.sqrt(dt) * random_shocks[:, t-1])
        
    final_prices = paths[:, -1]
    
    # Probabilidad de cerrar por encima del target (YES en Polymarket)
    wins = np.sum(final_prices > target_price)
    prob_yes = wins / simulations
    
    return prob_yes


def stable_monte_carlo_probability(current_price, target_price, time_to_expiry_sec, volatility, drift):
    """
    Reduce sesgo de tendencia mezclando:
    - escenario con drift estimado (30%)
    - escenario neutro drift=0 (70%)
    """
    p_with_drift = monte_carlo_probability(current_price, target_price, time_to_expiry_sec, volatility, drift)
    p_neutral = monte_carlo_probability(current_price, target_price, time_to_expiry_sec, volatility, 0.0)
    return 0.30 * p_with_drift + 0.70 * p_neutral

def scan_and_trade(client):
    log.info("🔍 Escaneando mercados para simulación Monte Carlo...")
    now = datetime.now(timezone.utc)
    
    # Filtro: Mercados que acaban en los próximos 25 mins
    min_dt = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    max_dt = (now + timedelta(minutes=25)).strftime('%Y-%m-%dT%H:%M:%SZ')
    url = f"{GAMMA_API}/events?limit=50&tag_id=102892&active=true&closed=false&end_date_min={min_dt}&end_date_max={max_dt}"
    
    try:
        events = requests.get(url, timeout=10).json()
    except Exception as e:
        log.error(f"Falla de API Gamma: {e}")
        return

    # Diagnóstico: cuántos eventos pasan filtros (ticker + priceToBeat o fallback Binance + ventana 3–20 min)
    n_events = len(events) if isinstance(events, list) else 0
    n_with_target = 0
    n_in_window = 0
    for ev in events or []:
        title = ev.get("title", "").lower()
        ticker = "BTC" if "bitcoin" in title or "btc" in title else "ETH" if "ethereum" in title or "eth" in title else None
        if not ticker:
            continue
        meta = ev.get("eventMetadata") or {}
        ptb = float(meta.get("priceToBeat", 0))
        if ptb <= 0:
            start_str = ev.get("startTime") or (ev.get("markets") or [{}])[0].get("eventStartTime") or ev.get("startDate")
            if start_str:
                ptb = get_binance_historical(ticker, start_str)
        if ptb <= 0:
            continue
        n_with_target += 1
        for mkt in ev.get("markets", []):
            try:
                end_dt = datetime.fromisoformat(mkt.get("endDate", "").replace("Z", "+00:00"))
                time_left_sec = (end_dt - now).total_seconds()
                if 180 <= time_left_sec <= 1200:
                    n_in_window += 1
                    break
            except Exception:
                pass
    if n_events > 0 and (n_with_target == 0 or n_in_window == 0):
        log.info(f"📊 Eventos: {n_events} | Con target BTC/ETH: {n_with_target} | En ventana 3–20 min: {n_in_window} (edge>{MIN_EDGE_REQUIRED:.0%})")

    for ev in events or []:
        title = ev.get("title", "").lower()
        ticker = "BTC" if "bitcoin" in title or "btc" in title else "ETH" if "ethereum" in title or "eth" in title else None
        
        if not ticker:
            continue
        meta = ev.get("eventMetadata") or {}
        price_to_beat_raw = float(meta.get("priceToBeat", 0))
        # Fallback: la API a menudo no envía eventMetadata; usar precio Binance al inicio del intervalo 5m
        if price_to_beat_raw <= 0:
            start_str = ev.get("startTime") or (ev.get("markets") or [{}])[0].get("eventStartTime") or ev.get("startDate")
            if start_str:
                price_to_beat_raw = get_binance_historical(ticker, start_str)
                if price_to_beat_raw > 0:
                    log.debug(f"Target desde Binance al inicio: {ticker} ${price_to_beat_raw:.2f}")
        if price_to_beat_raw <= 0:
            continue
        
        for mkt in ev.get("markets", []):
            try:
                end_dt = datetime.fromisoformat(mkt.get("endDate").replace('Z', '+00:00'))
                time_left_sec = (end_dt - now).total_seconds()
                
                # ESTRATEGIA: Ventana 3–20 min + evitar entrada demasiado temprano.
                if time_left_sec < 180 or time_left_sec > 1200:
                    continue
                start_str = mkt.get("eventStartTime") or ev.get("startTime") or ev.get("startDate")
                if start_str:
                    try:
                        start_dt = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                        age_sec = (now - start_dt).total_seconds()
                        if age_sec < MIN_MARKET_AGE_SEC:
                            log.info(f"⏳ [{ticker}] Skip early-entry: age={age_sec:.0f}s (<{MIN_MARKET_AGE_SEC}s)")
                            continue
                    except Exception:
                        pass
                
                mkt_id = mkt.get("id")
                if mkt_id in TRADED_MARKETS:
                    continue
                
                tids = json.loads(mkt.get("clobTokenIds", "[]"))
                if len(tids) < 2: continue
                
                # Obtener info Binance
                current_px, vol, drift = get_binance_data(ticker)
                if not current_px: continue
                
                price_to_beat = price_to_beat_raw
                
                # Simulación robusta: blend neutral + drift para reducir sesgo.
                mc_prob_yes = stable_monte_carlo_probability(current_px, price_to_beat, time_left_sec, vol, drift)
                mc_prob_no = 1.0 - mc_prob_yes
                
                log.info(f"🎲 [{ticker}] MC Sim -> YES: {mc_prob_yes:.1%} | NO: {mc_prob_no:.1%} | Quedan {time_left_sec:.0f}s (Dif: {current_px - price_to_beat:.2f})")
                
                # Obtener order book
                book_yes = client.get_order_book(tids[0])
                book_no = client.get_order_book(tids[1])
                
                ask_yes = min([float(a.price) for a in book_yes.asks]) if book_yes.asks else 0.99
                ask_no = min([float(a.price) for a in book_no.asks]) if book_no.asks else 0.99
                
                # Edge de YES
                edge_yes = mc_prob_yes - ask_yes - EDGE_BUFFER
                edge_no = mc_prob_no - ask_no - EDGE_BUFFER
                
                side_to_buy = None
                best_edge = 0
                ask_price = 0
                token_to_buy = None
                
                # NO vs YES: lo decide solo el edge (prob MC vs libro). Si precio actual < priceToBeat,
                # la simulación suele dar prob_yes < 50% → edge en NO. Si precio > target → más YES.
                if edge_yes > MIN_EDGE_REQUIRED and ask_yes < 0.85:
                    side_to_buy = "YES"
                    best_edge = edge_yes
                    ask_price = ask_yes
                    token_to_buy = tids[0]
                    mc_prob = mc_prob_yes
                elif edge_no > MIN_EDGE_REQUIRED and ask_no < 0.85:
                    side_to_buy = "NO"
                    best_edge = edge_no
                    ask_price = ask_no
                    token_to_buy = tids[1]
                    mc_prob = mc_prob_no

                if not side_to_buy and (edge_yes > 0 or edge_no > 0):
                    log.info(f"   Edge insuficiente (mín {MIN_EDGE_REQUIRED:.0%}): YES={edge_yes:.2%} ask={ask_yes:.2f} | NO={edge_no:.2%} ask={ask_no:.2f}")
                    
                if side_to_buy:
                    log.info(f"🚀 ALERTA EDGE PROFUNDO: Comprar {side_to_buy} a {ask_price:.3f} | Prob Real: {mc_prob:.3f} | Edge: {best_edge:.3f}")
                    
                    # Kelly Criterion Cauteloso
                    # f* = Edge / Odds
                    odds = (1.0 - ask_price) / ask_price
                    kelly_f = best_edge / odds
                    
                    # Fraction Kelly (Mitad para ser seguros)
                    safe_kelly = max(0.01, min(kelly_f * 0.5, MAX_RISK_PER_TRADE))
                    
                    # Obtener balance real para el cálculo
                    try:
                        rpc_list = ["https://polygon-bor-rpc.publicnode.com", "https://rpc.ankr.com/polygon"]
                        current_bankroll = 1.0
                        for rpc in rpc_list:
                            try:
                                w3 = Web3(Web3.HTTPProvider(rpc))
                                usdc_abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
                                usdc_contract = w3.eth.contract(address=Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"), abi=usdc_abi)
                                bal_raw = usdc_contract.functions.balanceOf(Web3.to_checksum_address(PROXY_ADDRESS)).call()
                                current_bankroll = bal_raw / 1e6
                                if current_bankroll > 0: break
                            except: continue
                    except Exception:
                        current_bankroll = 1.0

                    # Escala por saldo: < 20 → 1 USD; >= 20 → 2 USD; cada 10 USD más → +1 USD (tope MAX_TRADE_USD_CAP)
                    cap_usd, max_shares = get_trade_size_usd(current_bankroll)
                    trade_usd = cap_usd

                    if trade_usd > current_bankroll or current_bankroll < 5.0:
                        log.warning(f"Saldo insuficiente: trade ${trade_usd} > balance ${current_bankroll:.2f}")
                        continue

                    shares = round(trade_usd / ask_price, 2)
                    shares = min(shares, max_shares)
                    trade_usd = round(shares * ask_price, 2)
                    if shares < 1.0:
                        shares = 1.0
                        trade_usd = round(shares * ask_price, 2)
                    if trade_usd > cap_usd:
                        trade_usd = cap_usd
                        shares = min(round(trade_usd / ask_price, 2), max_shares)
                        trade_usd = round(shares * ask_price, 2)
                    if shares > max_shares:
                        shares = max_shares
                        trade_usd = round(shares * ask_price, 2)

                    # Log de auditoría
                    log.info(f"💰 Ejecutando trade: ${trade_usd:.2f} USD ({shares} shares @ {ask_price:.3f}) en {side_to_buy} | Bankroll: ${current_bankroll:.2f} (tamaño máx ${cap_usd:.2f})")
                    
                    try:
                        order = client.create_order(OrderArgs(price=ask_price, size=shares, side="BUY", token_id=token_to_buy))
                        resp = client.post_order(order)
                        log.info(f"✅ ORDEN REAL ENVIADA: {resp}")
                        msg = f"🚀 COMPRA EJECUTADA\nMercado: {ticker} ({side_to_buy})\nPrecio: {ask_price} | {shares} shares\nInversión: ${trade_usd:.2f} USD (máx ${cap_usd:.2f} por saldo ${current_bankroll:.0f})\nProb MC: {mc_prob:.1%}"
                        send_telegram(msg)
                        
                        # Auditoría: registrar en disco ANTES de añadir a TRADED_MARKETS (orden único)
                        log_trade_to_file({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "market_id": mkt_id,
                            "market": ticker,
                            "side": side_to_buy,
                            "price": ask_price,
                            "investment": trade_usd,
                            "shares": shares,
                            "prob_mc": mc_prob,
                            "order_id": resp.get("orderID"),
                        })
                        TRADED_MARKETS.add(mkt_id)
                        save_traded_markets()
                    except Exception as e:
                        log.error(f"Falla ejecutando orden real: {e}")
                    
                    # Pausa para no spam y límite de exposición (v5.8 - Cooldown 30s)
                    # Solo permitimos UN trade por ciclo de escaneo para evitar stacking.
                    time.sleep(30)
                    return # Salimos del escaneo actual tras operar

            except Exception as e:
                log.error(f"Error procesando mercado: {e}")

RESOLUTION_CHECK_SEC = 300  # Actualizar resultados de mercados resueltos cada 5 min

def main():
    log.info("Iniciando Motor Cuantitativo Monte Carlo de Polymarket...")
    lock_fd = acquire_lock()
    load_traded_markets()
    client = get_clob_client()
    last_resolution_check = 0.0
    try:
        while True:
            auto_redeem_if_needed(client)
            now_sec = time.time()
            if now_sec - last_resolution_check >= RESOLUTION_CHECK_SEC:
                update_trades_resolutions()
                last_resolution_check = now_sec
            scan_and_trade(client)
            time.sleep(3)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass

if __name__ == "__main__":
    main()
