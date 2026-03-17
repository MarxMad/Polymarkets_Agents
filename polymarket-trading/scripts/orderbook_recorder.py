import os
import json
import time
import logging
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("OrderbookRecorder")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

GAMMA_API = "https://gamma-api.polymarket.com"

TAG_ID = int(os.getenv("OB_TAG_ID", "102892"))  # 5m crypto binaries
LIMIT_PRICE = float(os.getenv("OB_LIMIT_PRICE", "0.30"))  # umbral de "barato"
POLL_SECONDS = float(os.getenv("OB_POLL_SECONDS", "1.0"))

WINDOW_MIN_SEC = int(os.getenv("OB_WINDOW_MIN_SEC", "60"))     # registrar cerca del final
WINDOW_MAX_SEC = int(os.getenv("OB_WINDOW_MAX_SEC", "1200"))   # 20 min

OUT_FILE = os.path.expanduser(os.getenv("OB_OUT_FILE", "~/orderbook_snapshots.jsonl"))


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def build_client() -> ClobClient:
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not pk:
        raise RuntimeError("Falta POLYMARKET_PRIVATE_KEY en ~/.openclaw/.env")
    creds = ApiCreds(
        os.getenv("POLYMARKET_API_KEY"),
        os.getenv("POLYMARKET_API_SECRET"),
        os.getenv("POLYMARKET_API_PASSPHRASE"),
    )
    proxy = os.getenv("PROXY_ADDRESS")
    return ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds, signature_type=2, funder=proxy)


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


def append_jsonl(obj: dict):
    os.makedirs(os.path.dirname(OUT_FILE) or ".", exist_ok=True)
    with open(OUT_FILE, "a") as f:
        f.write(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n")


def main():
    client = build_client()
    log.info(f"Grabando snapshots a {OUT_FILE} | poll={POLL_SECONDS}s | umbral={LIMIT_PRICE:.2f} | ventana={WINDOW_MIN_SEC}-{WINDOW_MAX_SEC}s")

    while True:
        now = datetime.now(timezone.utc)
        try:
            events = get_events(now)
        except Exception as e:
            log.warning(f"Gamma error: {e}")
            time.sleep(2.0)
            continue

        tracked = 0
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
                except Exception as e:
                    log.debug(f"CLOB book error: {e}")
                    continue

                yes_bid, yes_ask = best_prices(book_yes)
                no_bid, no_ask = best_prices(book_no)
                cheap_yes = (yes_ask is not None and yes_ask <= LIMIT_PRICE)
                cheap_no = (no_ask is not None and no_ask <= LIMIT_PRICE)

                append_jsonl({
                    "ts": _utc_now_iso(),
                    "ticker": ticker,
                    "event_id": ev.get("id"),
                    "market_id": mkt.get("id"),
                    "question": mkt.get("question"),
                    "endDate": end_s,
                    "time_left_s": round(tleft, 3),
                    "token_yes": str(token_yes),
                    "token_no": str(token_no),
                    "yes_bid": yes_bid,
                    "yes_ask": yes_ask,
                    "no_bid": no_bid,
                    "no_ask": no_ask,
                    "cheap_yes": cheap_yes,
                    "cheap_no": cheap_no,
                })
                tracked += 1

        if tracked == 0:
            log.info("No hay mercados BTC/ETH en ventana para grabar.")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()

