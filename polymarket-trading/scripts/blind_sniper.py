import os
import time
import json
import csv
import logging
from datetime import datetime, timezone
import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("BlindSniper")

GAMMA_API = "https://gamma-api.polymarket.com"
STATS_FILE = os.path.join(os.path.dirname(__file__), "blind_stats.csv")

# Ensure stats file has headers
if not os.path.exists(STATS_FILE):
    with open(STATS_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["timestamp", "market", "entry_price", "exit_price", "size_shares", "result", "profit_usd", "duration_sec"])

def get_clob_client():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
    
    host = "https://clob.polymarket.com"
    chain_id = 137
    
    from py_clob_client.clob_types import ApiCreds
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    client = ClobClient(host, key=pk, chain_id=chain_id, creds=creds)
    return client

def notify_telegram(message):
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": chat_id, "text": message}, timeout=10)
    except:
        pass

def save_stat(market, entry_price, exit_price, size_shares, result, profit_usd, duration_sec):
    with open(STATS_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.now().isoformat(),
            market,
            f"{entry_price:.3f}",
            f"{exit_price:.3f}",
            f"{size_shares}",
            result,
            f"{profit_usd:.3f}",
            duration_sec
        ])

def discover_new_binary_opportunity():
    # Buscamos los mercados que ACABAN de empezar
    # Tags: 102892(5M)
    # IMPORTANTE: Usamos endDate ASC para encontrar los de "ahora" y no los de mañana.
    params = {"active": "true", "closed": "false", "limit": 250, "tag_id": 102892, "order": "endDate", "ascending": "true"}
    resp = requests.get(f"{GAMMA_API}/events", params=params)
    events = resp.json() if resp.status_code == 200 else []
    
    now = datetime.now(timezone.utc)
    
    for event in events:
        for mkt in event.get("markets", []):
            if not mkt.get("active") or mkt.get("closed"): continue
            
            question = mkt.get("question", "")
            if "Up or Down" not in question and "5 min" not in question: continue
            
            tokens = json.loads(mkt.get("clobTokenIds", "[]"))
            if len(tokens) < 2: continue
            
            # Checamos si esta nuevo nuevo (empezó hace menos de 80 segundos)
            start_date_str = mkt.get("startDate")
            if start_date_str:
                try:
                    start_dt = datetime.fromisoformat(start_date_str.replace('Z', '+00:00'))
                    diff = (now - start_dt).total_seconds()
                    
                    # Validar también que el mercado termine pronto (hoy, no mañana)
                    end_date_str = mkt.get("endDate")
                    end_dt = datetime.fromisoformat(end_date_str.replace('Z', '+00:00')) if end_date_str else None
                    time_to_expiry = (end_dt - now).total_seconds() if end_dt else 999999
                    
                    # Solo lo tomamos si acaba de arrancar Y termina en menos de 30 minutos
                    if 0 <= diff <= 120 and time_to_expiry < 1800:
                        return {
                            "question": question,
                            "token_yes": tokens[0],
                            "token_no": tokens[1],
                            "start_dt": start_dt
                        }
                except Exception:
                    pass
    return None

def main():
    log.info("🎯 Iniciando Blind Sniper ($0.10) 🎯")
    client = get_clob_client()
    
    mkt = discover_new_binary_opportunity()
    if not mkt:
        log.info("No se encontraron mercados de 5M RECIÉN ABIERTOS.")
        return
        
    log.info(f"⚡ Mercado Fresco Detectado: {mkt['question']}")
    
    try:
        log.info("⏳ Esperando inyección de liquidez de Creadores de Mercado (Bypass Protección Algorítmica)...")
        ask_yes = None
        target_ask_threshold = 0.70 
        
        for wait_sec in range(25): # Monitorear por ~37 segundos
            book = client.get_order_book(mkt["token_yes"])
            if book.asks:
                current_ask = min([float(a.price) for a in book.asks])
                if current_ask <= target_ask_threshold:
                    ask_yes = current_ask
                    break
            time.sleep(1.5)
            
        if not ask_yes:
            log.warning("Muro falso de precios altos o spread vacío. Los MM no soltaron el mercado. Abortando.")
            time.sleep(30)
            return
            
        # Compramos ciegamente YES por $0.10
        trade_usd = 0.10
        trade_shares = round(trade_usd / ask_yes, 2)
        if trade_shares < 0.1: return # Muy pequeño
        
        target_price = ask_yes * 1.30
        if target_price > 0.99: target_price = 0.99
        target_price = round(target_price, 3)
        
        log.info(f"🛒 COMPRA CIEGA: {trade_shares} Shares a ${ask_yes:.3f} | Target: ${target_price:.3f}")
        notify_telegram(f"🙈 **BLIND SNIPER ACTIVO**\nMercado: {mkt['question']}\nPagando (Bid): ${ask_yes:.3f}\nInversión: $0.10 USD\nTarget Venta: ${target_price:.3f}")
        
        resp = {}
        for retry in range(5):
            try:
                order = client.create_order(OrderArgs(price=ask_yes, size=trade_shares, side="BUY", token_id=mkt["token_yes"]))
                resp = client.post_order(order)
                if resp.get("success"):
                    break
            except Exception as e:
                log.warning(f"Error Polymarket API propaganding (Reintentando en 3s): {e}")
                time.sleep(3)
                
        if not resp.get("success"):
            log.error(f"Fallo definitivo compra ciega tras reintentos: {resp}")
            time.sleep(60) # Evitar spam looping del mismo mercado fresco
            return
            
        # Monitorear venta
        start_time = time.time()
        for i in range(150): # ~ 5 minutos
            try:
                book = client.get_order_book(mkt["token_yes"])
                best_bid = float(book.bids[0].price) if book.bids else 0.0
                
                if best_bid >= target_price:
                    duration = round(time.time() - start_time, 1)
                    profit = (best_bid * trade_shares) - trade_usd
                    log.info(f"✅ BLIND TARGET ALCANZADO: Vendiendo a {best_bid}")
                    order = client.create_order(OrderArgs(price=best_bid, size=trade_shares, side="SELL", token_id=mkt["token_yes"]))
                    client.post_order(order)
                    
                    save_stat(mkt["question"], ask_yes, best_bid, trade_shares, "WIN_30%", profit, duration)
                    notify_telegram(f"✅ **BLIND WIN!**\nDuración: {duration}s\nGanancia: ${profit:.3f} USD")
                    return
            except Exception as e:
                pass
            time.sleep(2)
            
        duration = round(time.time() - start_time, 1)
        save_stat(mkt["question"], ask_yes, 0.0, trade_shares, "NATURAL_EXPIRY", -trade_usd, duration)
        log.info("⏰ Tiempo expirado para Blind Sniper. Pasa a resolución natural.")
        notify_telegram("⏰ Blind Expirado. Se fue a resolución natural.")
        
    except Exception as e:
        log.error(f"Error general: {e}")

if __name__ == "__main__":
    main()
