import os
import time
import threading
import json
import logging
import math
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds
from web3 import Web3
from eth_account import Account

# CONFIGURACIÓN HYPER SWARM V16 - "DIRECTIONAL SNIPER"
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("HyperSwarm-v16")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Global State for Swarm Intelligence
active_positions = set()
active_tickers = {}
closed_market_ids = set() 
KELLY_FRACTION = 0.20  # Re-ajustado para Sniper
BINANCE_PRICES = {"BTC": 0.0, "ETH": 0.0, "SOL": 0.0, "XRP": 0.0}
LAST_HEARTBEAT = 0
LAST_CLAIM = 0
claim_lock = threading.Lock()

# ABIs para Redención
SAFE_ABI = [
    {"inputs":[],"name":"nonce","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"name":"success","type":"bool"}],"type":"function"}
]
CTF_ABI = [{"constant":False,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"type":"function"}]

def notify_telegram(message):
    log.info(f"📤 Telegram Notification: {message[:100]}...")
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        resp = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
        if resp.status_code != 200:
            log.error(f"❌ Telegram API Error: {resp.text}")
    except Exception as e:
        log.error(f"❌ Telegram Connection Error: {e}")

def get_rpc():
    rpcs = ["https://polygon-bor-rpc.publicnode.com", "https://polygon.meowrpc.com"]
    for rpc in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 8}))
            if w3.is_connected(): return w3
        except: continue
    return None

def binance_watcher():
    """ Swarm Agent 1: Background real-time price tracker """
    while True:
        try:
            # BTC, ETH, SOL, XRP
            symbols = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]
            for s in symbols:
                ticker = s.replace("USDT", "")
                r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={s}", timeout=2).json()
                BINANCE_PRICES[ticker] = float(r.get("price", 0.0))
            time.sleep(0.5) # 500ms update
        except: time.sleep(2)

def get_binance_historical(ticker, iso_str):
    try:
        ts = int(datetime.fromisoformat(iso_str.replace('Z', '+00:00')).timestamp() * 1000)
        symbol = f"{ticker}USDT"
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1m&startTime={ts}&limit=1"
        r = requests.get(url, timeout=3).json()
        return float(r[0][1]) # Open price of that minute
    except: return 0.0

def get_wallet_balance():
    abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
    try:
        w3 = get_rpc()
        contract = w3.eth.contract(address=Web3.to_checksum_address(USDC_E_ADDRESS), abi=abi)
        bal = contract.functions.balanceOf(Web3.to_checksum_address(PROXY_ADDRESS)).call()
        return float(bal) / 1e6
    except: return 0.0

def get_token_balance(token_id):
    abi = [{"constant": True, "inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
    try:
        w3 = get_rpc()
        contract = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=abi)
        return float(contract.functions.balanceOf(Web3.to_checksum_address(PROXY_ADDRESS), int(token_id)).call()) / 1e6
    except: return 0.0

def get_vpin_and_prices(client, mkt, tid1, tid2):
    try:
        # PULL CORRECT PRICES DIRECTLY FROM GAMMA API (Matches UI exactly)
        best_ask = mkt.get("bestAsk")
        best_bid = mkt.get("bestBid")
        
        ask1 = float(best_ask) if best_ask is not None else 1.0
        ask2 = round(1.0 - float(best_bid), 4) if best_bid is not None else 1.0
        
        total_ask = ask1 + ask2
        
        # Pull CLOB only for Toxicity detection (VPIN) based on pending volume
        try:
            b1 = client.get_order_book(tid1)
            v_buy = sum([float(b.size) * float(b.price) for b in b1.bids]) if b1.bids else 0
            v_sell = sum([float(a.size) * float(a.price) for a in b1.asks]) if b1.asks else 0
            vpin = abs(v_buy - v_sell) / (v_buy + v_sell) if (v_buy + v_sell) > 0 else 0.0
        except: vpin = 0.5 # Neutral if CLOB fails
        
        return vpin, ask1, ask2, total_ask
    except: return 1.0, 1.0, 1.0, 2.0

def monitor_quantum(client, token_id, ticker, entry_p, market_id):
    target_p = round(entry_p * 1.15, 3)
    if target_p > 0.99: target_p = 0.99
    log.info(f"🧬 [v15] {ticker} mon. TP: ${target_p}")
    while True:
        try:
            shares = get_token_balance(token_id)
            if shares < 0.05: break
            book = client.get_order_book(token_id)
            if book.bids:
                best_bid = max([float(b.price) for b in book.bids])
                if best_bid >= target_p or best_bid > 0.95:
                    client.post_order(client.create_order(OrderArgs(price=best_bid, size=int(shares*100)/100.0, side="SELL", token_id=token_id)))
                    notify_telegram(f"💰 **HYPER SWARM PROFIT**\n{ticker} vendido a ${best_bid}.")
                    closed_market_ids.add(market_id)
                    break
            r = requests.get(f"{GAMMA_API}/markets?id={market_id}").json()
            if r and (r[0].get("closed") or r[0].get("resolved")): break
            time.sleep(3)
        except: time.sleep(10)
    active_positions.discard(token_id)
    if token_id in active_tickers: del active_tickers[token_id]

def claim_earnings(client):
    if not claim_lock.acquire(blocking=False):
        return
    log.info("\U0001f4b0 Buscando ganancias para redimir...")
    try:
        trades = client.get_trades()
        all_cids = []
        seen = set()
        for t in reversed(trades):
            cid = t.get('market')
            if cid and cid not in seen:
                all_cids.append(cid)
                seen.add(cid)
                if len(all_cids) >= 10: break

        to_redeem = []
        for cid in all_cids:
            try:
                r = requests.get(f"{GAMMA_API}/markets?condition_id={cid}", timeout=5).json()
                if r and r[0].get("closed"):
                    to_redeem.append(cid)
            except: continue

        if not to_redeem:
            log.info("\u2139\ufe0f No hay mercados cerrados para cobrar.")
            return

        w3 = get_rpc()
        if not w3:
            log.error("No RPC available for claim")
            return
        pk = os.getenv("POLYMARKET_PRIVATE_KEY")
        account = Account.from_key(pk)

        safe = w3.eth.contract(address=Web3.to_checksum_address(PROXY_ADDRESS), abi=SAFE_ABI)
        ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)

        # Use 'pending' to include unconfirmed txs and avoid nonce gaps
        current_nonce = w3.eth.get_transaction_count(account.address, 'pending')
        gas_price = int(w3.eth.gas_price * 1.5)

        # Process only ONE redemption per cycle to avoid nonce collisions
        cid = to_redeem[0]
        log.info(f"\U0001f680 Redimiendo {cid[:10]}... Nonce: {current_nonce}")
        data = ctf.encode_abi("redeemPositions", [
            Web3.to_checksum_address(USDC_E_ADDRESS),
            "0x" + "0" * 64,
            cid,
            [1, 2]
        ])

        sig = ("0x000000000000000000000000"
               + account.address[2:].lower()
               + "0000000000000000000000000000000000000000000000000000000000000000"
               + "01")

        tx = safe.functions.execTransaction(
            Web3.to_checksum_address(CTF_ADDRESS), 0, data, 0, 0, 0, 0,
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
            Web3.to_bytes(hexstr=sig)
        ).build_transaction({
            'from': account.address,
            'nonce': current_nonce,
            'gas': 200000,
            'gasPrice': gas_price
        })

        signed = w3.eth.account.sign_transaction(tx, private_key=pk)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        log.info(f"\u2705 Redenci\u00f3n enviada: {tx_hash.hex()}")

        # Wait for confirmation before returning
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status == 1:
                log.info(f"\u2705 Redenci\u00f3n confirmada! Gas: {receipt.gasUsed}")
                notify_telegram(f"\U0001f4b0 **AUTO-CLAIM OK**\nCondition: {cid[:12]}...\nHash: {tx_hash.hex()[:16]}...")
            else:
                log.warning(f"\u26a0\ufe0f Redenci\u00f3n revertida (gas insuficiente o ya redimido)")
        except Exception as e:
            log.warning(f"Timeout esperando confirmaci\u00f3n: {e}")

        remaining = len(to_redeem) - 1
        if remaining > 0:
            log.info(f"\u23f3 Quedan {remaining} mercados por cobrar (pr\u00f3ximo ciclo)")

    except Exception as e:
        log.error(f"\U0001f534 Error redimiendo: {e}")
    finally:
        claim_lock.release()


def main():
    global LAST_HEARTBEAT
    creds = ApiCreds(os.getenv("POLYMARKET_API_KEY"), os.getenv("POLYMARKET_API_SECRET"), os.getenv("POLYMARKET_API_PASSPHRASE"))
    client = ClobClient(CLOB_API, key=os.getenv("POLYMARKET_PRIVATE_KEY"), chain_id=137, creds=creds, signature_type=2, funder=PROXY_ADDRESS)
    
    balance = get_wallet_balance()
    log.info(f"🛰️ SWARM SNIPER v19 | Cash: ${balance:.2f} | BTC: ${BINANCE_PRICES.get('BTC', 0):.2f} | Tickers: {list(active_tickers.values())}")
    
    # Heartbeat every 30 minutes
    if time.time() - LAST_HEARTBEAT > 1800:
        notify_telegram(f"🎯 **SNIPER v19 ONLINE**\nCash: `${balance:.2f}`\nActive: `{list(active_tickers.values())}`\nModo: Sniper Compounding ($5)")
        LAST_HEARTBEAT = time.time()
        
    # Auto-claim every 8 minutes in background
    global LAST_CLAIM
    if time.time() - LAST_CLAIM > 480:
        threading.Thread(target=claim_earnings, args=(client,), daemon=True).start()
        LAST_CLAIM = time.time()

    if balance < 1.0 or len(active_positions) >= 3: return 

    try:
        now = datetime.now(timezone.utc)
        max_dt = (now + timedelta(minutes=30)).strftime('%Y-%m-%dT%H:%M:%SZ')
        
        # ✅ Usar el tag "5M" (ID 102892) con ventana de tiempo LIVE
        min_dt = now.strftime('%Y-%m-%dT%H:%M:%SZ')
        url = f"{GAMMA_API}/events?limit=50&tag_id=102892&active=true&closed=false&end_date_min={min_dt}&end_date_max={max_dt}"
        events = requests.get(url).json()
        
        log.info(f"📡 Eventos 5M en ventana: {len(events)}")
        
        # Recopilar markets directamente del evento (Dato Maestro)
        resp = []
        for ev in events:
            # Extraer Meta-Data del evento
            meta = ev.get("eventMetadata", {})
            price_to_beat = float(meta.get("priceToBeat", 0))
            
            start_str = ev.get("startTime")
            # 🔄 Fallback: Si no hay priceToBeat, buscar el precio histórico en Binance al momento de apertura
            if price_to_beat == 0 and start_str:
                 title = ev.get("title", "").lower()
                 ev_ticker = "BTC" if "btc" in title or "bitcoin" in title else "ETH" if "eth" in title else "SOL" if "sol" in title else "XRP"
                 price_to_beat = get_binance_historical(ev_ticker, start_str)
                 if price_to_beat > 0:
                     log.info(f"📜 Baseline histórica {ev_ticker}: ${price_to_beat}")
            
            if price_to_beat == 0: continue 
            
            if start_str:
                try:
                    start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                    if start_dt > now: continue 
                except: pass
            
            for m in ev.get("markets", []):
                # Guardar el price_to_beat en el mercado para el sniper
                m["_price_to_beat"] = price_to_beat
                resp.append(m)
        
        if len(resp) > 0:
            log.info(f"🎯 Sniper scan: {len(resp)} mercados")
        
        scanned_one = False
        for mkt in resp:
            m_id = str(mkt.get("id"))
            if m_id in closed_market_ids: continue
            
            p2b = mkt["_price_to_beat"]
            q = mkt.get("question", "").lower()
            ticker = "BTC" if "bitcoin" in q or "btc" in q else "ETH" if "eth" in q else "SOL" if "sol" in q else "XRP"
            
            try:
                end_dt = datetime.fromisoformat(mkt.get("endDate").replace('Z', '+00:00'))
                time_left = (end_dt - now).total_seconds()
                # 🎯 OPTION 2: "Terminal Sniper v18" (Últimos 80 segundos)
                if time_left < 10 or time_left > 80: continue
            except: continue
            
            tids = json.loads(mkt.get("clobTokenIds", "[]"))
            if len(tids) < 2: continue
            
            # SWARM AGENT 2: Directional Price Delta
            fast_price = BINANCE_PRICES.get(ticker, 0.0)
            if fast_price == 0: continue
            
            # DISTANCIA AL OBJETIVO: ¿Binance está lejos del precio base?
            delta = (fast_price - p2b) / p2b
            
            # Lógica SNIPER v19 (Compounding Phase)
            # Solo operamos en los últimos 80 segundos con una señal fuerte
            if not (10 <= time_left <= 85): 
                continue
                
            delta_threshold = 0.0015 # 0.15% de diferencia mínima con Binance
            max_buy_price = 0.90    # No compramos por encima de 0.90 (mínimo 11% ROI)
            
            target_side = None
            if delta > delta_threshold: target_side = "UP"
            elif delta < -delta_threshold: target_side = "DOWN"
            
            if not target_side: continue
            
            # Obtener el precio real del token según el lado elegido (siempre el mejor disponible)
            chosen_tid = tids[0] if target_side == "UP" else tids[1]
            try:
                # El midpoint nos da el precio más real y bajo disponible actualmente
                mid = float(client.get_midpoint(chosen_tid).get('mid', 0))
                entry_price = mid if mid > 0 else 1.0
            except: continue
                
            if entry_price > max_buy_price or entry_price < 0.10:
                continue # Demasiado caro (mal ROI) o sin liquidez
            
            # TRADE SIZE: $5.00 USD (Iniciando Fase de Interés Compuesto)
            trade_amount = 5.0
            if balance < 5.0: trade_amount = balance
            if trade_amount < 1.0: continue # Mínimo $1 para operar
            
            size = round(trade_amount / entry_price, 2)
            
            # Profit estimado basado en el precio real de entrada
            est_profit = round(((1.0 - entry_price) / entry_price) * 100, 1) if entry_price > 0 else 0
            log.info(f"💣 TERMINAL SNIPE v19! {ticker} {target_side} | Delta: {delta:.4f} | Est. Profit: {est_profit}% | Entry: ${entry_price:.3f} | Quedan: {int(time_left)}s")
            
            # Ejecutar orden al precio de mercado (usamos 0.99 para asegurar ejecución de lo disponible)
            client.post_order(client.create_order(OrderArgs(price=0.99, size=size, side="BUY", token_id=chosen_tid)))
            
            active_positions.add(chosen_tid)
            active_tickers[chosen_tid] = ticker
            closed_market_ids.add(m_id) # No repetir este mercado
            
            # Monitor directional TP/SL
            threading.Thread(target=monitor_quantum, args=(client, chosen_tid, ticker, entry_price, m_id), daemon=True).start()
            
            notify_telegram(f"🎯 **SNIPER v16 ACTION**\nTicker: {ticker}\nSide: **{target_side}**\nDelta: {delta:.4f}\nEntry: ${entry_price}\nSize: ${trade_amount:.2f}")
            return 

    except Exception as e:
        log.error(f"🔴 Error en main loop: {e}")

if __name__ == "__main__":
    # Start Swarm Watcher
    threading.Thread(target=binance_watcher, daemon=True).start()
    
    # Allowance check (only approve if needed, avoid wasting gas on every startup)
    try:
        w3 = get_rpc()
        pk = os.getenv("POLYMARKET_PRIVATE_KEY")
        account = Account.from_key(pk)
        proxy = Web3.to_checksum_address(PROXY_ADDRESS)
        spender = Web3.to_checksum_address("0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296")
        token = Web3.to_checksum_address(USDC_E_ADDRESS)

        allowance_abi = [{"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"name":"","type":"uint256"}],"type":"function"}]
        token_contract = w3.eth.contract(address=token, abi=allowance_abi)
        current_allowance = token_contract.functions.allowance(proxy, spender).call()

        if current_allowance > 10**12:
            log.info(f"Allowance OK ({current_allowance / 1e6:.0f} USDC). Skipping approval.")
        else:
            log.info(f"Allowance bajo ({current_allowance}). Aprobando...")
            approve_abi = [{"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"name":"","type":"bool"}],"type":"function"}]
            contract = w3.eth.contract(address=token, abi=approve_abi)
            safe = w3.eth.contract(address=proxy, abi=SAFE_ABI)
            data = contract.encode_abi("approve", [spender, 2**256 - 1])
            sig = "0x000000000000000000000000" + account.address[2:].lower() + "0" * 64 + "01"
            tx = safe.functions.execTransaction(
                token, 0, data, 0, 0, 0, 0,
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000000",
                Web3.to_bytes(hexstr=sig)
            ).build_transaction({
                'from': account.address,
                'nonce': w3.eth.get_transaction_count(account.address, 'pending'),
                'gas': 150000,
                'gasPrice': int(w3.eth.gas_price * 1.3)
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            log.info("Allowance aprobado exitosamente.")
    except Exception as e:
        log.warning(f"Allowance check: {e}")

    notify_telegram("🏎️ **v19: COMPOUNDING SNIPER ONLINE**\nModo: Sniper de Interés Compuesto ($5.00).\nAuto-Claim Ganancias: Activado (8m).")
    while True:
        try: main()
        except: pass
        time.sleep(3) # Ultra-fast cycle
