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

LAST_REDEEM_TIME = 0

def auto_redeem_if_needed(client):
    # Temporalmente desactivado mientras verificamos la función correcta en la librería
    pass

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("MonteCarloSniper")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

# Telegram Config
TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT_ID: return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": f"🛡️ SNIPER: {msg}"}, timeout=5)
    except:
        pass

GAMMA_API = "https://gamma-api.polymarket.com"
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")

# Parámetros de Crecimiento Acelerado (v5.7 - Objetivo $1M)
MAX_TRADE_USD = 10.00     # PROTEGIDO: $10.00 para balance de $315
MAX_RISK_PER_TRADE = 0.05 # Máximo 5% del bankroll real
MIN_EDGE_REQUIRED = 0.07  # AJUSTADO: 7% de ventaja (Mayor frecuencia de trades)
SIMULACIONES = 10000      
TRADED_MARKETS = set()    # MEMORIA: No repetir mercados (también persistido en disco)

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
        
        closes = [float(k[4]) for k in r_kl]
        returns = np.diff(closes) / closes[:-1] # Retornos porcentuales por minuto
        
        # Volatilidad anualizada (asumiendo 525600 minutos en un año)
        volatility = np.std(returns) * np.sqrt(525600)
        
        # Drift (tendencia a corto plazo)
        drift = np.mean(returns) * 525600
        
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

    for ev in events:
        title = ev.get("title", "").lower()
        ticker = "BTC" if "bitcoin" in title or "btc" in title else "ETH" if "ethereum" in title or "eth" in title else None
        
        if not ticker: continue
        meta = ev.get("eventMetadata") or {}
        price_to_beat_raw = float(meta.get("priceToBeat", 0))
        
        for mkt in ev.get("markets", []):
            try:
                end_dt = datetime.fromisoformat(mkt.get("endDate").replace('Z', '+00:00'))
                time_left_sec = (end_dt - now).total_seconds()
                
                # ESTRATEGIA v4.4: Solo 10-15 mins (Ignorar bajo 7 mins)
                # Esto da tiempo a que la estadística se estabilice.
                if time_left_sec < 420 or time_left_sec > 1200:
                    continue
                
                mkt_id = mkt.get("id")
                if mkt_id in TRADED_MARKETS:
                    continue
                
                tids = json.loads(mkt.get("clobTokenIds", "[]"))
                if len(tids) < 2: continue
                
                # Obtener info Binance
                current_px, vol, drift = get_binance_data(ticker)
                if not current_px: continue
                
                price_to_beat = price_to_beat_raw if price_to_beat_raw > 0 else current_px
                
                # Simular! (v5.1 Motor de Máxima Velocidad: 100% de tendencia)
                # Seguimos la tendencia real al 100% para capturar momentum.
                mc_prob_yes = monte_carlo_probability(current_px, price_to_beat, time_left_sec, vol, drift) 
                mc_prob_no = 1.0 - mc_prob_yes
                
                log.info(f"🎲 [{ticker}] MC Sim -> YES: {mc_prob_yes:.1%} | NO: {mc_prob_no:.1%} | Quedan {time_left_sec:.0f}s (Dif: {current_px - price_to_beat:.2f})")
                
                # Obtener order book
                book_yes = client.get_order_book(tids[0])
                book_no = client.get_order_book(tids[1])
                
                ask_yes = min([float(a.price) for a in book_yes.asks]) if book_yes.asks else 0.99
                ask_no = min([float(a.price) for a in book_no.asks]) if book_no.asks else 0.99
                
                # Edge de YES
                edge_yes = mc_prob_yes - ask_yes
                edge_no = mc_prob_no - ask_no
                
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
                        # Usar Web3 directo para el balance (más fiable para el proxy)
                        rpc_list = ["https://polygon-bor-rpc.publicnode.com", "https://rpc.ankr.com/polygon"]
                        current_bankroll = 1.0 # Default conservador
                        for rpc in rpc_list:
                            try:
                                w3 = Web3(Web3.HTTPProvider(rpc))
                                usdc_abi = [{"constant":True,"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"balance","type":"uint256"}],"type":"function"}]
                                usdc_contract = w3.eth.contract(address=Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"), abi=usdc_abi)
                                bal_raw = usdc_contract.functions.balanceOf(Web3.to_checksum_address(PROXY_ADDRESS)).call()
                                current_bankroll = bal_raw / 1e6
                                if current_bankroll > 0: break
                            except: continue
                    except:
                        current_bankroll = 1.0 
                    
                    # CAP ESTRICTO: máximo MAX_TRADE_USD ($10) en coste Y máximo 10 shares (lo que muestra Polymarket como "Apuesta")
                    # Así "Apuesta" en la UI nunca supera $10 y el coste real tampoco.
                    max_shares = 10.0
                    trade_usd = MAX_TRADE_USD

                    if trade_usd > current_bankroll or trade_usd < 1.0:
                        log.warning(f"Saldo insuficiente: trade ${trade_usd} > balance ${current_bankroll}")
                        continue

                    shares = round(trade_usd / ask_price, 2)
                    # Nunca más de 10 shares: evita que Polymarket muestre "Apuesta: $19" (notional = shares)
                    shares = min(shares, max_shares)
                    trade_usd = round(shares * ask_price, 2)

                    # Mínimo 5 shares para que el CLOB acepte (sin superar 10)
                    if shares < 5.0:
                        shares = min(5.0, max_shares)
                        trade_usd = round(shares * ask_price, 2)
                        log.info(f"Ajustando a mínimo 5 shares. Inversión: ${trade_usd:.2f}")

                    # Tope final: por si acaso, nunca más de MAX_TRADE_USD ni más de max_shares
                    if trade_usd > MAX_TRADE_USD:
                        trade_usd = MAX_TRADE_USD
                        shares = min(round(trade_usd / ask_price, 2), max_shares)
                        trade_usd = round(shares * ask_price, 2)
                    if shares > max_shares:
                        shares = max_shares
                        trade_usd = round(shares * ask_price, 2)

                    # Log de auditoría
                    log.info(f"💰 Ejecutando trade: ${trade_usd:.2f} USD ({shares} shares @ {ask_price:.3f}) en {side_to_buy} | Bankroll: ${current_bankroll:.2f}")
                    
                    # Descomentar para activar trading real:
                    try:
                        order = client.create_order(OrderArgs(price=ask_price, size=shares, side="BUY", token_id=token_to_buy))
                        resp = client.post_order(order)
                        log.info(f"✅ ORDEN REAL ENVIADA: {resp}")
                        msg = f"🚀 COMPRA EJECUTADA\nMercado: {ticker} ({side_to_buy})\nPrecio: {ask_price} | {shares} shares\nInversión: ${trade_usd:.2f} USD (máx $10)\nProb MC: {mc_prob:.1%}"
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

def main():
    log.info("Iniciando Motor Cuantitativo Monte Carlo de Polymarket...")
    lock_fd = acquire_lock()
    load_traded_markets()
    client = get_clob_client()
    try:
        while True:
            auto_redeem_if_needed(client)
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
