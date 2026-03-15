#!/usr/bin/env python3
"""
PumaClaw — Contrarian Scalper v2 (ESTABLE — solo bid, sin midpoint)
Buy the losing side early in 5-min binary markets, sell on price swings before close.
v2: Fixed sell bugs, min share enforcement, cancelación de órdenes parciales.
TP/SL evaluados con BID únicamente (no midpoint). Esta es la versión que funcionaba.
Mejoras TP: monitor cada 1s (antes 3s+scan), vender 99% shares para evitar fee balance.
"""

import os, sys, json, time, csv, math, threading, logging, requests, fcntl, atexit, signal
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, OrderArgs, BalanceAllowanceParams, AssetType
from py_clob_client.order_builder.constants import BUY, SELL

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("Scalper")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
PROXY = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT = os.getenv("TELEGRAM_CHAT_ID")

SAFE_ABI = [
    {"inputs": [], "name": "nonce", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"}, {"name": "operation", "type": "uint8"},
                {"name": "safeTxGas", "type": "uint256"}, {"name": "baseGas", "type": "uint256"},
                {"name": "gasPrice", "type": "uint256"}, {"name": "gasToken", "type": "address"},
                {"name": "refundReceiver", "type": "address"}, {"name": "signatures", "type": "bytes"}],
     "name": "execTransaction", "outputs": [{"name": "success", "type": "bool"}], "type": "function"},
]

ERC1155_APPROVAL_ABI = [
    {"inputs": [{"name": "account", "type": "address"}, {"name": "operator", "type": "address"}],
     "name": "isApprovedForAll", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
    {"inputs": [{"name": "operator", "type": "address"}, {"name": "approved", "type": "bool"}],
     "name": "setApprovalForAll", "outputs": [], "type": "function"},
]

ERC1155_BALANCE_ABI = [
    {"inputs": [{"name": "account", "type": "address"}, {"name": "id", "type": "uint256"}],
     "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
]

# ══════════════════════════════════════════════════════════════════
# STRATEGY PARAMETERS
# ══════════════════════════════════════════════════════════════════
ENTRY_WIN_LO = 150          # enter when 150-240s remain
ENTRY_WIN_HI = 240
MIN_BUY = 0.12              # min share price
MAX_BUY = 0.35              # max share price
TP_PCT = 0.30               # take profit at +30% (was 60% — capture wins before liquidity dies)
SL_PCT = 0.90               # stop loss at -90% (give reversal time, sells work now)
EXIT_SECS = 40              # forced exit 40s before close
AMOUNT = 5.00               # $5 per trade — ensures >10 shares at any entry price
MIN_SHARES = 5              # CLOB minimum for sell orders
TICKERS = {"BTC", "ETH", "SOL", "XRP"}
CYCLE_SEC = 3

# ══════════════════════════════════════════════════════════════════
# SINGLE INSTANCE LOCK
# ══════════════════════════════════════════════════════════════════
LOCK_FILE = "/tmp/contrarian_scalper.lock"
_lock_fd = None

def _release_lock():
    global _lock_fd
    if _lock_fd is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
        except Exception:
            pass
        _lock_fd = None
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass

def acquire_lock():
    global _lock_fd
    _lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, BlockingIOError):
        log.error(f"FATAL: Another scalper instance is already running (lock: {LOCK_FILE})")
        _lock_fd.close()
        _lock_fd = None
        sys.exit(1)
    _lock_fd.write(str(os.getpid()))
    _lock_fd.flush()
    atexit.register(_release_lock)
    signal.signal(signal.SIGTERM, lambda s, f: (_release_lock(), sys.exit(0)))

# ══════════════════════════════════════════════════════════════════
# STATE
# ══════════════════════════════════════════════════════════════════
positions = []
done_markets = set()
BP = {"BTC": 0.0, "ETH": 0.0, "SOL": 0.0, "XRP": 0.0}
hb_time = 0
mon_time = 0
stats = {"w": 0, "l": 0, "pnl": 0.0, "n": 0}
sell_failures = {}  # tid -> consecutive failure count

TICKER_MAP = {
    "btc": "BTC", "bitcoin": "BTC",
    "eth": "ETH", "ethereum": "ETH",
    "sol": "SOL", "solana": "SOL",
    "xrp": "XRP",
}

CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scalper_trades.csv")


# ══════════════════════════════════════════════════════════════════
# ON-CHAIN BALANCE CHECK
# ══════════════════════════════════════════════════════════════════
def check_token_balance(token_id):
    """Check actual on-chain balance for a conditional token."""
    try:
        w3 = get_rpc()
        if not w3:
            return None
        proxy = Web3.to_checksum_address(PROXY)
        ctf = w3.eth.contract(
            address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_BALANCE_ABI
        )
        token_int = int(token_id)
        balance_wei = ctf.functions.balanceOf(proxy, token_int).call()
        return balance_wei / 1e6  # CTF uses 6 decimals
    except Exception as e:
        log.warning(f"Balance check failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════
# P&L TRACKING
# ══════════════════════════════════════════════════════════════════
def init_csv():
    if not os.path.exists(CSV_PATH):
        with open(CSV_PATH, "w", newline="") as f:
            csv.writer(f).writerow([
                "ts", "ticker", "side", "entry", "exit", "shares",
                "cost", "revenue", "pnl", "pnl_pct", "reason", "held_s"
            ])


def record(pos, exit_px, reason, shares_sold=None):
    sh = shares_sold if shares_sold is not None else pos["sh"]
    rev = round(exit_px * sh, 4)
    pnl = round(rev - pos["cost"], 4)
    pct = round((pnl / pos["cost"]) * 100, 1) if pos["cost"] > 0 else 0
    held = int((datetime.now(timezone.utc) - pos["t0"]).total_seconds())
    with open(CSV_PATH, "a", newline="") as f:
        csv.writer(f).writerow([
            datetime.now(timezone.utc).isoformat(), pos["tk"], pos["sd"],
            pos["ep"], exit_px, sh, pos["cost"], rev, pnl, pct, reason, held
        ])
    stats["n"] += 1
    stats["pnl"] = round(stats["pnl"] + pnl, 2)
    if pnl >= 0:
        stats["w"] += 1
    else:
        stats["l"] += 1
    return pnl, pct


# ══════════════════════════════════════════════════════════════════
# TELEGRAM
# ══════════════════════════════════════════════════════════════════
def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=5
        )
    except:
        pass


# ══════════════════════════════════════════════════════════════════
# BINANCE FEED
# ══════════════════════════════════════════════════════════════════
def feed_loop():
    while True:
        try:
            for s in ["BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT"]:
                r = requests.get(
                    f"https://api.binance.com/api/v3/ticker/price?symbol={s}", timeout=5
                ).json()
                BP[s.replace("USDT", "")] = float(r.get("price", 0))
        except:
            pass
        time.sleep(2)


def hist_price(tk, ts):
    try:
        dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        r = requests.get(
            f"https://api.binance.com/api/v3/klines?symbol={tk}USDT"
            f"&interval=1m&startTime={int(dt.timestamp() * 1000)}&limit=1",
            timeout=5
        ).json()
        return float(r[0][1]) if r else 0
    except:
        return 0


def parse_ticker(text):
    text = text.lower()
    return next((v for k, v in TICKER_MAP.items() if k in text), None)


def get_rpc():
    for rpc in ["https://polygon-bor-rpc.publicnode.com", "https://polygon.meowrpc.com"]:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={'timeout': 8}))
            if w3.is_connected():
                return w3
        except:
            continue
    return None


def approve_ctf_for_selling():
    """Approve CTF Exchange + NegRisk contracts to transfer our conditional tokens."""
    w3 = get_rpc()
    if not w3:
        log.warning("No RPC — skipping CTF approval")
        return

    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    account = Account.from_key(pk)
    proxy = Web3.to_checksum_address(PROXY)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_APPROVAL_ABI)
    safe = w3.eth.contract(address=proxy, abi=SAFE_ABI)

    operators = [CTF_EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER]
    for op_addr in operators:
        op = Web3.to_checksum_address(op_addr)
        try:
            approved = ctf.functions.isApprovedForAll(proxy, op).call()
            if approved:
                log.info(f"CTF approval OK for {op_addr[:10]}...")
                continue

            log.info(f"Setting CTF approval for {op_addr[:10]}...")
            data = ctf.encode_abi("setApprovalForAll", [op, True])
            sig = "0x000000000000000000000000" + account.address[2:].lower() + "0" * 64 + "01"
            tx = safe.functions.execTransaction(
                Web3.to_checksum_address(CTF_ADDRESS), 0, data, 0, 0, 0, 0,
                "0x0000000000000000000000000000000000000000",
                "0x0000000000000000000000000000000000000000",
                Web3.to_bytes(hexstr=sig)
            ).build_transaction({
                'from': account.address,
                'nonce': w3.eth.get_transaction_count(account.address, 'pending'),
                'gas': 150000,
                'gasPrice': int(w3.eth.gas_price * 1.3),
            })
            signed = w3.eth.account.sign_transaction(tx, private_key=pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            log.info(f"CTF approval set for {op_addr[:10]}...")
        except Exception as e:
            log.warning(f"CTF approval error for {op_addr[:10]}: {e}")


# ══════════════════════════════════════════════════════════════════
# SELL LOGIC
# ══════════════════════════════════════════════════════════════════
def do_sell(client, pos, reason):
    tid = pos["tid"]
    # Use CLOB balance so we never sell more than we have (avoids "not enough balance" from fee deduction)
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
        b_info = client.get_balance_allowance(params)
        balance = float(b_info.get("balance", 0)) / 1e6 if isinstance(b_info, dict) else float(getattr(b_info, "balance", 0)) / 1e6
    except Exception as e:
        log.warning(f"Balance check failed for {pos['tk']}: {e}, using 95%% of pos")
        balance = pos["sh"] * 0.95
    if balance < 0.5:
        log.error(f"CLOB balance ~0 for {pos['tk']}, cannot sell")
        sell_failures[tid] = sell_failures.get(tid, 0) + 1
        if sell_failures.get(tid, 0) >= 3:
            log.error(f"ABANDON {pos['tk']} after 3 sell failures (no balance)")
            record(pos, 0, "SELL_FAILED")
            sell_failures.pop(tid, None)
            return True
        return False
    if balance < MIN_SHARES:
        sell_size = max(1.0, math.floor(balance * 100) / 100)
    else:
        sell_size = max(MIN_SHARES, math.floor(min(balance, pos["sh"]) * 100) / 100)
    try:
        book = client.get_order_book(tid)
        bid = max(float(b.price) for b in book.bids) if book.bids else 0.01
        order = client.create_order(OrderArgs(
            token_id=tid, price=bid, size=sell_size, side=SELL
        ))
        resp = client.post_order(order)

        sell_status = resp.get("status", "")
        sell_order_id = resp.get("orderID", "")

        if sell_status == "live":
            # Sell order placed but not filled — cancel it to free the tokens
            try:
                client.cancel(sell_order_id)
                log.warning(f"Sell not filled, cancelled {sell_order_id[:16]}... for {pos['tk']}")
            except:
                pass
            sell_failures[tid] = sell_failures.get(tid, 0) + 1
            return False

        if sell_status != "matched":
            log.warning(f"Sell order status '{sell_status}' for {pos['tk']}")
            sell_failures[tid] = sell_failures.get(tid, 0) + 1
            return False

        log.info(f"SELL response: {resp}")
        sell_failures.pop(tid, None)

        pnl, pct = record(pos, bid, reason, shares_sold=sell_size)
        log.info(
            f"{reason} | {pos['tk']} {pos['sd']} | "
            f"${pos['ep']:.3f} -> ${bid:.3f} | PnL ${pnl:+.2f} ({pct:+.1f}%)"
        )
        wr = (stats["w"] / (stats["w"] + stats["l"]) * 100) if (stats["w"] + stats["l"]) > 0 else 0
        tg(
            f"*SCALP {reason}* {'WIN' if pnl >= 0 else 'LOSS'} {pos['tk']}\n"
            f"Side: `{pos['sd']}`\n"
            f"Entry: `${pos['ep']:.3f}` -> Exit: `${bid:.3f}`\n"
            f"PnL: `${pnl:+.2f}` ({pct:+.1f}%)\n"
            f"Session: `{stats['w']}W/{stats['l']}L ({wr:.0f}%)` | `${stats['pnl']:+.2f}`"
        )
        return True
    except Exception as e:
        err = str(e)
        sell_failures[tid] = sell_failures.get(tid, 0) + 1
        fails = sell_failures[tid]

        if "not enough balance" in err or "Size" in err and "lower than the minimum" in err:
            if fails >= 3:
                log.error(f"ABANDON {pos['tk']} after {fails} sell failures: {e}")
                record(pos, 0, "SELL_FAILED")
                sell_failures.pop(tid, None)
                return True  # remove from positions
        else:
            log.error(f"Sell fail {pos['tk']} (attempt {fails}): {e}")
        return False


# ══════════════════════════════════════════════════════════════════
# MONITOR POSITIONS
# ══════════════════════════════════════════════════════════════════
def monitor(client):
    global mon_time
    now = datetime.now(timezone.utc)
    show_log = time.time() - mon_time > 30

    for pos in positions[:]:
        tl = (pos["t1"] - now).total_seconds()

        if tl <= 0:
            record(pos, 0, "EXPIRED")
            positions.remove(pos)
            sell_failures.pop(pos["tid"], None)
            tg(f"*SCALP EXPIRED* LOSS {pos['tk']}\nLost `${pos['cost']:.2f}`")
            continue

        try:
            book = client.get_order_book(pos["tid"])
            if not book.bids:
                if tl <= EXIT_SECS:
                    record(pos, 0, "NO_BIDS")
                    positions.remove(pos)
                    sell_failures.pop(pos["tid"], None)
                    tg(f"*SCALP NO BIDS* LOSS {pos['tk']}\nLost `${pos['cost']:.2f}`")
                continue
            bid = max(float(b.price) for b in book.bids)
        except:
            continue

        chg = (bid - pos["ep"]) / pos["ep"]

        reason = None
        if chg >= TP_PCT:
            reason = "TAKE_PROFIT"
        elif chg <= -SL_PCT:
            reason = "STOP_LOSS"
        elif tl <= EXIT_SECS:
            reason = "FORCE_EXIT"

        if reason:
            if do_sell(client, pos, reason):
                positions.remove(pos)
        elif show_log:
            log.info(
                f"HOLD | {pos['tk']} {pos['sd']} | "
                f"${bid:.3f} ({chg:+.1%}) | {int(tl)}s left"
            )

    if show_log:
        mon_time = time.time()


# ══════════════════════════════════════════════════════════════════
# SCAN & ENTER
# ══════════════════════════════════════════════════════════════════
def scan(client):
    if len(positions) >= 4:
        return

    now = datetime.now(timezone.utc)
    lo = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    hi = (now + timedelta(minutes=10)).strftime('%Y-%m-%dT%H:%M:%SZ')

    try:
        evts = requests.get(
            f"{GAMMA}/events?limit=50&tag_id=102892&active=true&closed=false"
            f"&end_date_min={lo}&end_date_max={hi}",
            timeout=10
        ).json()
    except:
        return

    for ev in evts:
        p2b = float(ev.get("eventMetadata", {}).get("priceToBeat", 0))
        ss = ev.get("startTime")

        if p2b == 0 and ss:
            t = parse_ticker(ev.get("title", ""))
            if not t:
                continue
            p2b = hist_price(t, ss)
        if p2b == 0:
            continue

        if ss:
            try:
                if datetime.fromisoformat(ss.replace('Z', '+00:00')) > now:
                    continue
            except:
                pass

        for m in ev.get("markets", []):
            mid = str(m.get("id"))
            if mid in done_markets:
                continue

            tk = parse_ticker(m.get("question", ""))
            if not tk or tk not in TICKERS:
                continue
            if any(p["tk"] == tk for p in positions):
                continue

            try:
                edt = datetime.fromisoformat(m.get("endDate").replace('Z', '+00:00'))
                tl = (edt - now).total_seconds()
                if tl < ENTRY_WIN_LO or tl > ENTRY_WIN_HI:
                    continue
            except:
                continue

            tids = json.loads(m.get("clobTokenIds", "[]"))
            if len(tids) < 2:
                continue

            px = BP.get(tk, 0)
            if px == 0:
                continue

            delta = (px - p2b) / p2b

            sd, tid = ("DOWN", tids[1]) if delta > 0 else ("UP", tids[0])

            try:
                book = client.get_order_book(tid)
                if not book.asks or not book.bids:
                    continue
                ask = min(float(a.price) for a in book.asks)
            except:
                continue

            if ask < MIN_BUY or ask > MAX_BUY:
                continue

            sh = round(AMOUNT / ask, 2)
            if sh < MIN_SHARES:
                log.info(f"SKIP {tk} — {sh:.1f} shares < min {MIN_SHARES}")
                done_markets.add(mid)
                continue

            try:
                order = client.create_order(OrderArgs(
                    token_id=tid, price=ask, size=sh, side=BUY
                ))
                resp = client.post_order(order)
                log.info(f"BUY response: {resp}")

                if resp.get("status") != "matched":
                    log.warning(f"Order not filled ({resp.get('status')}) — skipping {tk}")
                    done_markets.add(mid)
                    continue

                filled_size = float(resp.get("takingAmount", 0))
                order_id = resp.get("orderID", "")

                # Cancel remaining unfilled portion to avoid phantom positions
                if filled_size < sh and order_id:
                    try:
                        client.cancel(order_id)
                        log.info(f"Cancelled remaining order {order_id[:16]}... ({sh - filled_size:.1f} unfilled)")
                    except Exception as ce:
                        log.warning(f"Cancel failed (may be fully filled): {ce}")

                if filled_size < MIN_SHARES:
                    log.warning(f"Fill too small ({filled_size:.1f} < {MIN_SHARES}) for {tk} — skipping")
                    done_markets.add(mid)
                    continue

                filled_cost = float(resp.get("makingAmount", 0))
                actual_ep = round(filled_cost / filled_size, 4) if filled_size > 0 else ask

                time.sleep(3)  # wait for on-chain settlement

                pos = {
                    "mid": mid, "tid": tid, "tk": tk, "sd": sd,
                    "ep": actual_ep, "sh": filled_size,
                    "cost": round(filled_cost, 2),
                    "t0": datetime.now(timezone.utc), "t1": edt,
                }
                positions.append(pos)
                done_markets.add(mid)

                tp_px = round(actual_ep * (1 + TP_PCT), 3)
                sl_px = round(actual_ep * (1 - SL_PCT), 3)

                log.info(
                    f"ENTRY | {tk} {sd} | ${actual_ep:.3f} | {filled_size:.1f}sh | "
                    f"TP ${tp_px:.3f} SL ${sl_px:.3f} | {int(tl)}s left"
                )
                tg(
                    f"*SCALP ENTRY* {tk}\n"
                    f"Side: `{sd}` (contrarian)\n"
                    f"Entry: `${actual_ep:.3f}` x `{filled_size:.1f}` sh = `${pos['cost']:.2f}`\n"
                    f"TP: `${tp_px:.3f}` (+{TP_PCT * 100:.0f}%)\n"
                    f"SL: `${sl_px:.3f}` (-{SL_PCT * 100:.0f}%)\n"
                    f"Exit forzado: `{EXIT_SECS}s` antes del cierre\n"
                    f"Quedan: `{int(tl)}s`"
                )
            except Exception as e:
                log.error(f"Buy fail {tk}: {e}")


# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    acquire_lock()
    init_csv()

    threading.Thread(target=feed_loop, daemon=True).start()
    time.sleep(2)

    creds = ApiCreds(
        os.getenv("POLYMARKET_API_KEY"),
        os.getenv("POLYMARKET_API_SECRET"),
        os.getenv("POLYMARKET_API_PASSPHRASE"),
    )
    client = ClobClient(
        CLOB,
        key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        chain_id=137,
        creds=creds,
        signature_type=2,
        funder=PROXY,
    )

    log.info("Contrarian Scalper v2 starting...")

    try:
        resp = client.cancel_all()
        log.info(f"Cancelled all open orders: {resp}")
    except Exception as e:
        log.warning(f"Cancel all orders: {e}")

    approve_ctf_for_selling()
    tg(
        "*PumaClaw SCALPER v2 ONLINE*\n"
        f"Ventana entrada: `{ENTRY_WIN_LO}-{ENTRY_WIN_HI}s` restantes\n"
        f"Compra lado perdedor: `${MIN_BUY}-${MAX_BUY}`\n"
        f"TP: `+{TP_PCT * 100:.0f}%` | SL: `-{SL_PCT * 100:.0f}%`\n"
        f"Exit forzado: `{EXIT_SECS}s` antes del cierre\n"
        f"Min shares: `{MIN_SHARES}` | Max/trade: `${AMOUNT}`\n"
        f"Assets: `BTC, ETH, SOL, XRP`"
    )
    hb_time = time.time()
    loop_count = 0

    while True:
        try:
            monitor(client)
            loop_count += 1
            if loop_count % 3 == 0:
                scan(client)
                if len(done_markets) > 200:
                    done_markets.clear()

            if time.time() - hb_time > 1800:
                s = stats
                wr = (s["w"] / (s["w"] + s["l"]) * 100) if (s["w"] + s["l"]) > 0 else 0
                tg(
                    f"*SCALPER HEARTBEAT*\n"
                    f"Trades: `{s['n']}` | `{s['w']}W/{s['l']}L` ({wr:.0f}%)\n"
                    f"PnL: `${s['pnl']:+.2f}`\n"
                    f"Posiciones activas: `{len(positions)}`"
                )
                hb_time = time.time()
        except Exception as e:
            log.error(f"Loop: {e}")

        time.sleep(1)  # check TP/SL every 1s; scan every 3s
