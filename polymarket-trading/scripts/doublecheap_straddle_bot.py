import os
import sys
import json
import time
import logging
import fcntl
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("DoubleCheapStraddle")


def _env_file_path() -> str:
    return os.path.expanduser(os.getenv("POLY_ENV_FILE", "~/.openclaw/.env"))


load_dotenv(_env_file_path())

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Estrategia (Opción 2 "filtro" simplificada para trading real):
# - Espera primer cheap: una pierna con ask <= LIMIT
# - Dentro de CONFIRM_SEC, requiere que la otra pierna llegue a ask <= LIMIT + OTHER_WITHIN
# - Solo entra si EN EL MOMENTO de entrar ambas asks <= LIMIT + OTHER_WITHIN (compramos ambas piernas)
TAG_ID = int(os.getenv("OB_TAG_ID", "102892"))
LIMIT = float(os.getenv("STRADDLE_LIMIT", "0.35"))
OTHER_WITHIN = float(os.getenv("STRADDLE_OTHER_WITHIN", "0.02"))
CONFIRM_SEC = float(os.getenv("STRADDLE_CONFIRM_SEC", "60"))

WINDOW_MIN_SEC = int(os.getenv("OB_WINDOW_MIN_SEC", "60"))
WINDOW_MAX_SEC = int(os.getenv("OB_WINDOW_MAX_SEC", "1200"))
POLL_SECONDS = float(os.getenv("STRADDLE_POLL_SECONDS", "2.0"))

USD_PER_LEG = float(os.getenv("STRADDLE_USD_PER_LEG", "1.0"))
MAX_SHARES_PER_LEG = float(os.getenv("STRADDLE_MAX_SHARES_PER_LEG", "2.0"))

TRADES_LOG_FILE = os.path.expanduser(os.getenv("STRADDLE_TRADES_LOG_FILE", "~/trades_history_straddle.json"))
TRADED_MARKETS_FILE = os.path.expanduser(os.getenv("STRADDLE_TRADED_MARKETS_FILE", "~/.openclaw/workspace/skills/polymarket/traded_markets_straddle.json"))
LOCK_FILE = os.path.expanduser(os.getenv("STRADDLE_LOCK_FILE", "~/.openclaw/workspace/skills/polymarket/.straddle.lock"))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or (os.getenv("TELEGRAM_GROUP_IDS") or "").strip().split(",")[0].strip() or None


def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": f"🟦 STRADDLE: {msg}"}, timeout=5)
    except Exception:
        pass


def acquire_lock():
    try:
        dirname = os.path.dirname(LOCK_FILE)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname, exist_ok=True)
        fd = os.open(LOCK_FILE, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return fd
    except (OSError, BlockingIOError):
        log.error("Otra instancia del straddle ya está corriendo (lock activo). Salida.")
        sys.exit(1)


def load_traded_markets() -> set[str]:
    try:
        if os.path.exists(TRADED_MARKETS_FILE):
            with open(TRADED_MARKETS_FILE, "r") as f:
                data = json.load(f)
                mids = data.get("market_ids", [])
                return set(str(x) for x in mids)
    except Exception:
        pass
    return set()


def save_traded_markets(s: set[str]):
    try:
        dirname = os.path.dirname(TRADED_MARKETS_FILE)
        if dirname and not os.path.isdir(dirname):
            os.makedirs(dirname, exist_ok=True)
        with open(TRADED_MARKETS_FILE, "w") as f:
            json.dump({"market_ids": list(s), "updated": datetime.now(timezone.utc).isoformat()}, f, indent=2)
    except Exception as e:
        log.warning(f"No se pudo guardar traded_markets_straddle: {e}")


def log_trade(trade_data: dict):
    try:
        history = []
        if os.path.exists(TRADES_LOG_FILE):
            with open(TRADES_LOG_FILE, "r") as f:
                history = json.load(f)
        history.append(trade_data)
        with open(TRADES_LOG_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        log.warning(f"No se pudo escribir {TRADES_LOG_FILE}: {e}")


def build_client() -> ClobClient:
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not pk:
        raise RuntimeError(f"Falta POLYMARKET_PRIVATE_KEY en { _env_file_path() }")
    creds = ApiCreds(
        os.getenv("POLYMARKET_API_KEY"),
        os.getenv("POLYMARKET_API_SECRET"),
        os.getenv("POLYMARKET_API_PASSPHRASE"),
    )
    proxy = os.getenv("PROXY_ADDRESS")
    return ClobClient(CLOB, key=pk, chain_id=137, creds=creds, signature_type=2, funder=proxy)


def get_events(now_utc: datetime):
    min_dt = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    max_dt = (now_utc + timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{GAMMA_API}/events?limit=50&tag_id={TAG_ID}&active=true&closed=false&end_date_min={min_dt}&end_date_max={max_dt}"
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    data = r.json()
    return data if isinstance(data, list) else []


def best_prices(book):
    try:
        best_ask = min(float(a.price) for a in (book.asks or [])) if getattr(book, "asks", None) else None
    except Exception:
        best_ask = None
    try:
        best_bid = max(float(b.price) for b in (book.bids or [])) if getattr(book, "bids", None) else None
    except Exception:
        best_bid = None
    return best_bid, best_ask


def clamp_shares(usd: float, price: float) -> tuple[float, float]:
    if not price or price <= 0:
        return 0.0, 0.0
    shares = round(usd / price, 2)
    shares = min(shares, MAX_SHARES_PER_LEG)
    if shares < 1.0:
        shares = 1.0
    used = round(shares * price, 4)
    # cap duro por USD_PER_LEG (evitar subir notional por min shares)
    if used > usd:
        shares = max(1.0, round(usd / price, 2))
        used = round(shares * price, 4)
    return shares, used


def main():
    lock_fd = acquire_lock()
    traded = load_traded_markets()
    client = build_client()

    # estado del filtro: market_id -> {t0, first_side}
    pending = {}

    log.info(
        f"Straddle BOT (wallet={_env_file_path()}) | limit={LIMIT:.2f} other_within=+{OTHER_WITHIN:.2f} "
        f"confirm={CONFIRM_SEC:.0f}s | usd_per_leg={USD_PER_LEG} | poll={POLL_SECONDS}s"
    )
    send_telegram(
        f"Arrancó. limit={LIMIT:.2f}, other_within=+{OTHER_WITHIN:.2f}, confirm={CONFIRM_SEC:.0f}s, usd/leg={USD_PER_LEG}"
    )

    try:
        while True:
            now = datetime.now(timezone.utc)
            try:
                events = get_events(now)
            except Exception as e:
                log.warning(f"Gamma error: {e}")
                time.sleep(2.0)
                continue

            for ev in events:
                title = (ev.get("title") or "").lower()
                ticker = "BTC" if ("bitcoin" in title or "btc" in title) else "ETH" if ("ethereum" in title or "eth" in title) else None
                if not ticker:
                    continue

                for mkt in ev.get("markets", []) or []:
                    end_s = mkt.get("endDate")
                    if not end_s:
                        continue
                    try:
                        end_dt = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
                    except Exception:
                        continue
                    tleft = (end_dt - now).total_seconds()
                    if tleft < WINDOW_MIN_SEC or tleft > WINDOW_MAX_SEC:
                        continue

                    market_id = str(mkt.get("id"))
                    if market_id in traded:
                        continue

                    try:
                        tids = json.loads(mkt.get("clobTokenIds", "[]"))
                    except Exception:
                        tids = []
                    if len(tids) < 2:
                        continue
                    token_yes, token_no = tids[0], tids[1]

                    try:
                        book_yes = client.get_order_book(token_yes)
                        book_no = client.get_order_book(token_no)
                    except Exception:
                        continue

                    _, ask_yes = best_prices(book_yes)
                    _, ask_no = best_prices(book_no)
                    if ask_yes is None or ask_no is None:
                        continue

                    # marcar primer cheap
                    if market_id not in pending:
                        if ask_yes <= LIMIT:
                            pending[market_id] = {"t0": time.time(), "first": "YES"}
                        elif ask_no <= LIMIT:
                            pending[market_id] = {"t0": time.time(), "first": "NO"}
                        else:
                            continue

                    st = pending.get(market_id)
                    if not st:
                        continue

                    # expirar confirmación
                    if time.time() - st["t0"] > CONFIRM_SEC:
                        pending.pop(market_id, None)
                        continue

                    # confirmar "la otra pierna se acerca"
                    other_threshold = LIMIT + OTHER_WITHIN
                    if st["first"] == "YES":
                        if ask_no > other_threshold:
                            continue
                    else:
                        if ask_yes > other_threshold:
                            continue

                    # entrar SOLO si ambas asks están dentro del rango permitido
                    if ask_yes > other_threshold or ask_no > other_threshold:
                        continue

                    # sizing por pierna
                    shares_yes, usd_yes = clamp_shares(USD_PER_LEG, ask_yes)
                    shares_no, usd_no = clamp_shares(USD_PER_LEG, ask_no)
                    if shares_yes <= 0 or shares_no <= 0:
                        continue

                    q = (mkt.get("question") or "")[:90]
                    log.info(
                        f"🟦 DISPARO {ticker} mkt={market_id} | asks yes={ask_yes:.3f} no={ask_no:.3f} "
                        f"| buy_usd yes={usd_yes:.2f} no={usd_no:.2f} | tleft={tleft:.0f}s | {q}"
                    )

                    try:
                        o_yes = client.create_order(OrderArgs(price=ask_yes, size=shares_yes, side="BUY", token_id=token_yes))
                        r_yes = client.post_order(o_yes)
                        o_no = client.create_order(OrderArgs(price=ask_no, size=shares_no, side="BUY", token_id=token_no))
                        r_no = client.post_order(o_no)
                    except Exception as e:
                        log.warning(f"Fallo post_order: {e}")
                        pending.pop(market_id, None)
                        continue

                    trade = {
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "strategy": "doublecheap_straddle_filter",
                        "market": ticker,
                        "market_id": market_id,
                        "question": mkt.get("question"),
                        "params": {"limit": LIMIT, "other_within": OTHER_WITHIN, "confirm_sec": CONFIRM_SEC},
                        "yes": {"price": ask_yes, "shares": shares_yes, "usd": usd_yes, "order_id": (r_yes or {}).get("orderID"), "resp": r_yes},
                        "no": {"price": ask_no, "shares": shares_no, "usd": usd_no, "order_id": (r_no or {}).get("orderID"), "resp": r_no},
                    }
                    log_trade(trade)
                    send_telegram(
                        f"ENTRADA {ticker}\nYES {shares_yes}@{ask_yes:.3f} (${usd_yes:.2f})\n"
                        f"NO  {shares_no}@{ask_no:.3f} (${usd_no:.2f})\n"
                        f"mkt_id={market_id}"
                    )

                    traded.add(market_id)
                    save_traded_markets(traded)
                    pending.pop(market_id, None)

                    # cooldown para no spamear
                    time.sleep(15)

            time.sleep(POLL_SECONDS)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except Exception:
                pass


if __name__ == "__main__":
    main()

