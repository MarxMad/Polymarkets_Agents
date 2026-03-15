import os
import time
import threading
import json
import logging
import csv
from datetime import datetime, timezone, timedelta
import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds
from web3 import Web3
from eth_account import Account

# ═══════════════════════════════════════════════════════════════════
# PUMACLAW v20 — PRECISION COMPOUNDER
# ═══════════════════════════════════════════════════════════════════
# Changes from v19:
#   - Entry at midpoint price (not 0.99 market order)
#   - max_buy_price: 0.55 (min 82% ROI per win)
#   - delta_threshold: 0.25% (stronger signal, fewer but better trades)
#   - Compounding: 3% of bankroll (min $2, max $15)
#   - BTC + ETH only (most liquid, best Binance correlation)
#   - Time window: 30-120s before close (price cap is the real filter)
#   - Full P&L tracking with CSV log
# ═══════════════════════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("PumaClaw-v20")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── v20 Strategy Parameters ──────────────────────────────────────
DELTA_BASE = 0.0020            # base delta at 45s — scales up with time remaining
MAX_BUY_PRICE = 0.55           # only enter when ROI >= 82%
MIN_BUY_PRICE = 0.10           # avoid illiquid garbage
TIME_WINDOW_MIN = 45           # skip last 45s (prices already spiked)
TIME_WINDOW_MAX = 200           # sweet spot: shares at $0.50-$0.65
BET_PCT = 0.03                 # 3% of bankroll per trade
BET_MIN = 2.0                  # minimum $2 per trade
BET_MAX = 15.0                 # maximum $15 per trade (capital protection)
MAX_ACTIVE = 3                 # wider window = more opportunities
ALLOWED_TICKERS = {"BTC", "ETH", "SOL", "XRP"}  # liquid 5-min binary assets


import math

def scaled_delta(time_left_s):
    """More time left = need stronger signal to avoid reversals."""
    return DELTA_BASE * math.sqrt(time_left_s / 45.0)

# ── Global State ─────────────────────────────────────────────────
active_positions = set()
active_tickers = {}
closed_market_ids = set()
BINANCE_PRICES = {"BTC": 0.0, "ETH": 0.0, "SOL": 0.0, "XRP": 0.0}
LAST_HEARTBEAT = 0
LAST_CLAIM = 0
claim_lock = threading.Lock()

# ── P&L Tracking ─────────────────────────────────────────────────
TRADES_CSV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v20_trades.csv")
pnl_lock = threading.Lock()

def init_trades_csv():
    if not os.path.exists(TRADES_CSV):
        with open(TRADES_CSV, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "ticker", "side", "delta", "entry_price",
                "shares", "cost_usd", "time_left_s", "result", "payout_usd", "pnl_usd"
            ])

def log_trade(ticker, side, delta, entry_price, shares, cost_usd, time_left,
              result="PENDING", payout=0.0):
    with pnl_lock:
        with open(TRADES_CSV, "a", newline="") as f:
            csv.writer(f).writerow([
                datetime.now(timezone.utc).isoformat(), ticker, side,
                f"{delta:.5f}", f"{entry_price:.3f}", f"{shares:.2f}",
                f"{cost_usd:.2f}", int(time_left), result,
                f"{payout:.2f}", f"{payout - cost_usd:.2f}"
            ])

def get_session_stats():
    """Quick stats from the CSV for heartbeat reports."""
    if not os.path.exists(TRADES_CSV):
        return {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    with open(TRADES_CSV) as f:
        reader = list(csv.DictReader(f))
    wins = sum(1 for r in reader if r.get("result") == "WIN")
    losses = sum(1 for r in reader if r.get("result") == "LOSS")
    pending = sum(1 for r in reader if r.get("result") == "PENDING")
    pnl = sum(float(r.get("pnl_usd", 0)) for r in reader if r.get("result") in ("WIN", "LOSS"))
    return {"trades": len(reader), "wins": wins, "losses": losses, "pending": pending, "pnl": pnl}


# ── ABIs ─────────────────────────────────────────────────────────
SAFE_ABI = [
    {"inputs":[],"name":"nonce","outputs":[{"name":"","type":"uint256"}],"type":"function"},
    {"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],"name":"execTransaction","outputs":[{"name":"success","type":"bool"}],"type":"function"}
]
CTF_ABI = [{"constant":False,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"type":"function"}]


# ═══════════════════════════════════════════════════════════════════
# INFRASTRUCTURE (unchanged from v19)
# ═══════════════════════════════════════════════════════════════════

def notify_telegram(message):
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"}, timeout=5)
    except:
        pass

def get_rpc():
    rpcs = ["https://polygon-bor-rpc.publicnode.com", "https://polygon.meowrpc.com"]
    for rpc in rpcs:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 8}))
            if w3.is_connected(): return w3
        except: continue
    return None

def binance_watcher():
    while True:
        try:
            for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
                ticker = s.replace("USDT", "")
                r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={s}", timeout=2).json()
                BINANCE_PRICES[ticker] = float(r.get("price", 0.0))
            time.sleep(0.5)
        except: time.sleep(2)

def get_binance_historical(ticker, iso_str):
    try:
        ts = int(datetime.fromisoformat(iso_str.replace('Z', '+00:00')).timestamp() * 1000)
        url = f"https://api.binance.com/api/v3/klines?symbol={ticker}USDT&interval=1m&startTime={ts}&limit=1"
        r = requests.get(url, timeout=3).json()
        return float(r[0][1])
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


# ═══════════════════════════════════════════════════════════════════
# POSITION MONITOR — tracks outcome and logs P&L
# ═══════════════════════════════════════════════════════════════════

def monitor_position(client, token_id, ticker, entry_price, cost_usd, shares, market_id):
    """Watch position until market closes, then log WIN or LOSS."""
    log.info(f"[MON] {ticker} | Entry ${entry_price:.3f} | {shares:.1f} sh | Watching...")
    while True:
        try:
            current_shares = get_token_balance(token_id)
            if current_shares < 0.05:
                # Shares disappeared — either sold or market resolved to $0
                log_trade(ticker, "?", 0, entry_price, shares, cost_usd, 0,
                          result="LOSS", payout=0.0)
                log.info(f"[MON] {ticker} | Shares gone (likely LOSS or redeemed)")
                break

            # Check if market is closed/resolved
            try:
                r = requests.get(f"{GAMMA_API}/markets?id={market_id}", timeout=5).json()
                if r and (r[0].get("closed") or r[0].get("resolved")):
                    # Market resolved — shares still held means we might have WON
                    time.sleep(3)
                    final_shares = get_token_balance(token_id)
                    if final_shares > 0.05:
                        payout = final_shares * 1.0  # winning shares = $1 each
                        log_trade(ticker, "WIN", 0, entry_price, shares, cost_usd, 0,
                                  result="WIN", payout=payout)
                        log.info(f"[MON] {ticker} | WIN! Payout: ${payout:.2f} (profit: ${payout - cost_usd:.2f})")
                        notify_telegram(f"*WIN* {ticker}\nPayout: `${payout:.2f}`\nProfit: `${payout - cost_usd:+.2f}`")
                    else:
                        log_trade(ticker, "LOSS", 0, entry_price, shares, cost_usd, 0,
                                  result="LOSS", payout=0.0)
                        log.info(f"[MON] {ticker} | LOSS (${cost_usd:.2f})")
                        notify_telegram(f"*LOSS* {ticker}\nPerdida: `${cost_usd:.2f}`")
                    break
            except:
                pass

            time.sleep(5)
        except:
            time.sleep(10)

    active_positions.discard(token_id)
    if token_id in active_tickers:
        del active_tickers[token_id]


# ═══════════════════════════════════════════════════════════════════
# AUTO-CLAIM (from v19 fix, unchanged)
# ═══════════════════════════════════════════════════════════════════

def claim_earnings(client):
    if not claim_lock.acquire(blocking=False):
        return
    log.info("Buscando ganancias para redimir...")
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
            return

        w3 = get_rpc()
        if not w3: return
        pk = os.getenv("POLYMARKET_PRIVATE_KEY")
        account = Account.from_key(pk)
        safe = w3.eth.contract(address=Web3.to_checksum_address(PROXY_ADDRESS), abi=SAFE_ABI)
        ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)

        current_nonce = w3.eth.get_transaction_count(account.address, 'pending')
        gas_price = int(w3.eth.gas_price * 1.5)

        cid = to_redeem[0]
        log.info(f"Redimiendo {cid[:10]}... Nonce: {current_nonce}")
        data = ctf.encode_abi("redeemPositions", [
            Web3.to_checksum_address(USDC_E_ADDRESS), "0x" + "0" * 64, cid, [1, 2]
        ])
        sig = ("0x000000000000000000000000" + account.address[2:].lower()
               + "0" * 64 + "01")
        tx = safe.functions.execTransaction(
            Web3.to_checksum_address(CTF_ADDRESS), 0, data, 0, 0, 0, 0,
            "0x0000000000000000000000000000000000000000",
            "0x0000000000000000000000000000000000000000",
            Web3.to_bytes(hexstr=sig)
        ).build_transaction({
            'from': account.address, 'nonce': current_nonce,
            'gas': 200000, 'gasPrice': gas_price
        })
        signed = w3.eth.account.sign_transaction(tx, private_key=pk)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        try:
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if receipt.status == 1:
                log.info(f"Claim OK: {tx_hash.hex()[:16]}")
        except:
            pass
    except Exception as e:
        log.error(f"Error redimiendo: {e}")
    finally:
        claim_lock.release()


# ═══════════════════════════════════════════════════════════════════
# MAIN TRADING LOOP — v20 Precision Compounder
# ═══════════════════════════════════════════════════════════════════

def main():
    global LAST_HEARTBEAT, LAST_CLAIM

    creds = ApiCreds(os.getenv("POLYMARKET_API_KEY"), os.getenv("POLYMARKET_API_SECRET"), os.getenv("POLYMARKET_API_PASSPHRASE"))
    client = ClobClient(CLOB_API, key=os.getenv("POLYMARKET_PRIVATE_KEY"), chain_id=137, creds=creds, signature_type=2, funder=PROXY_ADDRESS)

    balance = get_wallet_balance()
    stats = get_session_stats()
    wr = (stats["wins"] / (stats["wins"] + stats["losses"]) * 100) if (stats["wins"] + stats["losses"]) > 0 else 0

    log.info(f"v20 | ${balance:.2f} | BTC ${BINANCE_PRICES.get('BTC',0):.0f} ETH ${BINANCE_PRICES.get('ETH',0):.0f} | W{stats['wins']}/L{stats['losses']} ({wr:.0f}%) | PnL ${stats['pnl']:+.2f}")

    # Heartbeat every 30 min
    if time.time() - LAST_HEARTBEAT > 1800:
        notify_telegram(
            f"*PumaClaw v20 ONLINE*\n"
            f"Cash: `${balance:.2f}`\n"
            f"Record: `{stats['wins']}W / {stats['losses']}L ({wr:.0f}%)`\n"
            f"PnL: `${stats['pnl']:+.2f}`\n"
            f"Modo: Precision Compounder (3% bankroll)"
        )
        LAST_HEARTBEAT = time.time()

    # Auto-claim every 8 min
    if time.time() - LAST_CLAIM > 480:
        threading.Thread(target=claim_earnings, args=(client,), daemon=True).start()
        LAST_CLAIM = time.time()

    if balance < BET_MIN or len(active_positions) >= MAX_ACTIVE:
        return

    # ── SCAN 5-minute binary markets ─────────────────────────────
    now = datetime.now(timezone.utc)
    min_dt = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    max_dt = (now + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%SZ')
    url = f"{GAMMA_API}/events?limit=50&tag_id=102892&active=true&closed=false&end_date_min={min_dt}&end_date_max={max_dt}"

    try:
        events = requests.get(url, timeout=10).json()
    except:
        return

    candidates = []
    for ev in events:
        meta = ev.get("eventMetadata", {})
        price_to_beat = float(meta.get("priceToBeat", 0))

        start_str = ev.get("startTime")
        if price_to_beat == 0 and start_str:
            title = ev.get("title", "").lower()
            ticker_map = {"btc": "BTC", "bitcoin": "BTC", "eth": "ETH", "ethereum": "ETH", "sol": "SOL", "solana": "SOL", "xrp": "XRP"}
            ev_ticker = next((v for k, v in ticker_map.items() if k in title), None)
            if ev_ticker is None:
                continue
            price_to_beat = get_binance_historical(ev_ticker, start_str)

        if price_to_beat == 0:
            continue

        if start_str:
            try:
                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                if start_dt > now: continue
            except: pass

        for m in ev.get("markets", []):
            m["_price_to_beat"] = price_to_beat
            candidates.append(m)

    # ── EVALUATE each candidate ──────────────────────────────────
    for mkt in candidates:
        m_id = str(mkt.get("id"))
        if m_id in closed_market_ids:
            continue

        q = mkt.get("question", "").lower()
        ticker_map = {"btc": "BTC", "bitcoin": "BTC", "eth": "ETH", "ethereum": "ETH", "sol": "SOL", "solana": "SOL", "xrp": "XRP"}
        ticker = next((v for k, v in ticker_map.items() if k in q), None)
        if ticker is None or ticker not in ALLOWED_TICKERS:
            continue

        try:
            end_dt = datetime.fromisoformat(mkt.get("endDate").replace('Z', '+00:00'))
            time_left = (end_dt - now).total_seconds()
            if time_left < TIME_WINDOW_MIN or time_left > TIME_WINDOW_MAX:
                continue
        except:
            continue

        tids = json.loads(mkt.get("clobTokenIds", "[]"))
        if len(tids) < 2:
            continue

        fast_price = BINANCE_PRICES.get(ticker, 0.0)
        if fast_price == 0:
            continue

        p2b = mkt["_price_to_beat"]
        delta = (fast_price - p2b) / p2b

        # ── DIRECTION FILTER (scaled delta) ──────────────────────
        req_delta = scaled_delta(time_left)
        target_side = None
        if delta > req_delta:
            target_side = "UP"
        elif delta < -req_delta:
            target_side = "DOWN"

        if not target_side:
            continue

        # ── PRICE CHECK via orderbook ────────────────────────────
        chosen_tid = tids[0] if target_side == "UP" else tids[1]
        try:
            book = client.get_order_book(chosen_tid)
            if not book.asks:
                continue
            best_ask = min(float(a.price) for a in book.asks)
            entry_price = best_ask
        except:
            continue

        if entry_price > MAX_BUY_PRICE or entry_price < MIN_BUY_PRICE:
            continue

        # ── COMPOUNDING BET SIZE ─────────────────────────────────
        trade_amount = round(balance * BET_PCT, 2)
        trade_amount = max(trade_amount, BET_MIN)
        trade_amount = min(trade_amount, BET_MAX)
        if trade_amount > balance * 0.10:  # hard safety: never more than 10% of bankroll
            trade_amount = round(balance * 0.10, 2)
        if trade_amount < BET_MIN:
            continue

        shares = round(trade_amount / entry_price, 2)
        if shares < 1.0:
            continue

        est_roi = round(((1.0 - entry_price) / entry_price) * 100, 1)

        log.info(
            f"v20 SNIPE | {ticker} {target_side} | Delta: {delta:.4f} (req {req_delta:.4f}) | "
            f"Entry: ${entry_price:.3f} | ROI: {est_roi}% | "
            f"${trade_amount:.2f} ({shares:.1f} sh) | {int(time_left)}s left"
        )

        # ── EXECUTE — limit order at best ask (NOT 0.99) ─────────
        try:
            order = client.create_order(OrderArgs(
                price=entry_price,
                size=shares,
                side="BUY",
                token_id=chosen_tid
            ))
            resp = client.post_order(order)
            success = resp.get("success", False) if isinstance(resp, dict) else False
        except Exception as e:
            log.error(f"Order failed: {e}")
            continue

        if not success:
            log.warning(f"Order rejected: {resp}")
            continue

        active_positions.add(chosen_tid)
        active_tickers[chosen_tid] = ticker
        closed_market_ids.add(m_id)

        log_trade(ticker, target_side, delta, entry_price, shares, trade_amount, time_left)

        # Monitor in background thread
        threading.Thread(
            target=monitor_position,
            args=(client, chosen_tid, ticker, entry_price, trade_amount, shares, m_id),
            daemon=True
        ).start()

        notify_telegram(
            f"*v20 SNIPE*\n"
            f"Ticker: `{ticker}` {target_side}\n"
            f"Delta: `{delta:.4f}` | Entry: `${entry_price:.3f}`\n"
            f"Size: `${trade_amount:.2f}` ({shares:.1f} sh)\n"
            f"ROI if win: `{est_roi}%`\n"
            f"Cash left: `${balance - trade_amount:.2f}`"
        )
        return  # one trade per cycle

    # No trade this cycle (normal — waiting for strong signal)


# ═══════════════════════════════════════════════════════════════════
# STARTUP
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    init_trades_csv()
    threading.Thread(target=binance_watcher, daemon=True).start()

    # Allowance check
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
            log.info(f"Allowance OK. Skipping approval.")
        else:
            log.info("Allowance bajo. Aprobando...")
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
                'gas': 150000, 'gasPrice': int(w3.eth.gas_price * 1.3)
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            log.info("Allowance aprobado.")
    except Exception as e:
        log.warning(f"Allowance check: {e}")

    notify_telegram(
        "*v20: PRECISION COMPOUNDER ONLINE*\n"
        f"Delta: `{DELTA_BASE*100:.2f}%` | Max price: `${MAX_BUY_PRICE}`\n"
        f"Bet: `{BET_PCT*100:.0f}%` bankroll (${BET_MIN}-${BET_MAX})\n"
        f"Assets: `{chr(44).join(sorted(ALLOWED_TICKERS))}`\n"
        f"Window: `{TIME_WINDOW_MIN}-{TIME_WINDOW_MAX}s` before close"
    )

    while True:
        try:
            main()
        except Exception as e:
            log.error(f"Main loop error: {e}")
        time.sleep(3)
