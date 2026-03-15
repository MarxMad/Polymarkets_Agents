import os
import time
import json
import logging
import math
import numpy as np
import requests
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds

# ═══════════════════════════════════════════════════════════════════
# MONTE CARLO SNIPER - RUTA AL MILLÓN ($50 -> $1M)
# ═══════════════════════════════════════════════════════════════════
# Estrategia: 
# 1. Escanea mercados binarios de 5-min/15-min de Cripto (BTC, ETH).
# 2. Calcula la Volatilidad Histórica (HV) real usando datos de Binance.
# 3. Simula 10,000 trayectorias de precio mediante Movimiento Browniano Geométrico (GBM).
# 4. Obtiene la "Probabilidad Verdadera" matemática.
# 5. Compara con los precios (asks) de Polymarket. Si hay un EDGE significativo, entra con Kelly Criterion.
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("MonteCarloSniper")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

GAMMA_API = "https://gamma-api.polymarket.com"
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")

# Parámetros de Riesgo (50 USD a 1M)
CAPITAL_INICIAL = 50.0 # Empezamos con gestión conservadora
MAX_RISK_PER_TRADE = 0.05 # Máximo 5% del bankroll
MIN_EDGE_REQUIRED = 0.15  # Requerimos un 15% de ventaja matemática para entrar (Margin of Safety)
SIMULACIONES = 10000      # Caminatas aleatorias de Monte Carlo

BINANCE_TICKERS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

def get_clob_client():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    creds = ApiCreds(
        os.getenv("POLYMARKET_API_KEY"),
        os.getenv("POLYMARKET_API_SECRET"),
        os.getenv("POLYMARKET_API_PASSPHRASE")
    )
    return ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=2, funder=PROXY_ADDRESS)

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
    
    # Filtro: Mercados que acaban en los próximos 15 mins (Binary)
    min_dt = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    max_dt = (now + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ')
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
        
        meta = ev.get("eventMetadata", {})
        price_to_beat = float(meta.get("priceToBeat", 0))
        if price_to_beat == 0: continue
        
        for mkt in ev.get("markets", []):
            try:
                end_dt = datetime.fromisoformat(mkt.get("endDate").replace('Z', '+00:00'))
                time_left_sec = (end_dt - now).total_seconds()
                
                # Ignorar si quedan menos de 30s (las subastas cierran la liquidez)
                if time_left_sec < 30 or time_left_sec > 900:
                    continue
                
                tids = json.loads(mkt.get("clobTokenIds", "[]"))
                if len(tids) < 2: continue
                
                # Obtener info Binance
                current_px, vol, drift = get_binance_data(ticker)
                if not current_px: continue
                
                # Simular!
                mc_prob_yes = monte_carlo_probability(current_px, price_to_beat, time_left_sec, vol, drift/10) # Suavizamos el drift a corto plazo
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
                    trade_usd = round(CAPITAL_INICIAL * safe_kelly, 2)
                    trade_usd = max(2.0, trade_usd) # Mínimo $2
                    shares = round(trade_usd / ask_price, 2)
                    
                    log.info(f"💰 Ejecutando trade via Kelly: ${trade_usd} usd ({shares} shares) en {side_to_buy}")
                    
                    # Descomentar para activar trading real:
                    '''
                    try:
                        order = client.create_order(OrderArgs(price=ask_price, size=shares, side="BUY", token_id=token_to_buy))
                        resp = client.post_order(order)
                        log.info(f"✅ Orden enviada: {resp}")
                    except Exception as e:
                        log.error(f"Falla ejecutando orden: {e}")
                    '''
                    
                    # Pausa para no spam
                    time.sleep(10)

            except Exception as e:
                log.error(f"Error procesando mercado: {e}")

def main():
    log.info("Iniciando Motor Cuantitativo Monte Carlo de Polymarket...")
    client = get_clob_client()
    
    while True:
        scan_and_trade(client)
        time.sleep(3)

if __name__ == "__main__":
    main()
