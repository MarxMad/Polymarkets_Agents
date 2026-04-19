import os
import sys
import json
import time
import logging
import fcntl
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

from web3 import Web3
from eth_account import Account

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, ApiCreds, BalanceAllowanceParams, AssetType


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("DoubleCheapStraddle")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)


def _env_file_path() -> str:
    cand = os.path.expanduser(os.getenv("POLY_ENV_FILE", "~/.openclaw/.env_straddle"))
    if os.path.exists(cand):
        return cand
    return os.path.expanduser("~/.openclaw/.env")


load_dotenv(_env_file_path())

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

# Estrategia (Opción 2 "filtro" + stop) (segura):
# - NO abre ninguna pierna en el primer cheap.
# - Primer cheap: memoriza cuál pierna tocó ask <= LIMIT y arranca el reloj.
# - Confirmación: dentro de CONFIRM_SEC la pierna opuesta llega a ask <= LIMIT + OTHER_WITHIN.
# - En confirmación: coloca BUY límite a LIMIT en AMBAS piernas (YES y NO).
# - Si no completa (no hay 2 piernas “llenadas”) en TIMEOUT_SEC:
#   cancela órdenes pendientes y, si alguna pierna quedó expuesta, vende al bid (stop).
TAG_ID = int(os.getenv("OB_TAG_ID", "102892"))
LIMIT = float(os.getenv("STRADDLE_LIMIT", "0.35"))
OTHER_WITHIN = float(os.getenv("STRADDLE_OTHER_WITHIN", "0.02"))
CONFIRM_SEC = float(os.getenv("STRADDLE_CONFIRM_SEC", "60"))
TIMEOUT_SEC = float(os.getenv("STRADDLE_TIMEOUT_SEC", "45"))

WINDOW_MIN_SEC = int(os.getenv("OB_WINDOW_MIN_SEC", "60"))
WINDOW_MAX_SEC = int(os.getenv("OB_WINDOW_MAX_SEC", "1200"))
POLL_SECONDS = float(os.getenv("STRADDLE_POLL_SECONDS", "2.0"))

USD_PER_LEG = float(os.getenv("STRADDLE_USD_PER_LEG", "2.0"))  # fijo en 2 USD/pierna
MAX_SHARES_PER_LEG = float(os.getenv("STRADDLE_MAX_SHARES_PER_LEG", "4.0"))

TRADES_LOG_FILE = os.path.expanduser(os.getenv("STRADDLE_TRADES_LOG_FILE", "~/trades_history_straddle.json"))
TRADED_MARKETS_FILE = os.path.expanduser(os.getenv("STRADDLE_TRADED_MARKETS_FILE", "~/.openclaw/workspace/skills/polymarket/traded_markets_straddle.json"))
LOCK_FILE = os.path.expanduser(os.getenv("STRADDLE_LOCK_FILE", "~/.openclaw/workspace/skills/polymarket/.straddle.lock"))

TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or (os.getenv("TELEGRAM_GROUP_IDS") or "").strip().split(",")[0].strip() or None

STRADDLE_STRATEGY_NAME = os.getenv("STRADDLE_STRATEGY_NAME", "STRADDLE O2-FILTRO v3")


def send_telegram(msg: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        return
    try:
        url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
        tag = f"🟦 {STRADDLE_STRATEGY_NAME}"
        requests.post(url, json={"chat_id": TG_CHAT_ID, "text": f"{tag}: {msg}"}, timeout=5)
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


def safe_post(client: ClobClient, side: str, token_id: str, price: float, size: float):
    order = client.create_order(OrderArgs(price=float(price), size=float(size), side=side, token_id=token_id))
    return client.post_order(order)


def _order_matched_size(client: ClobClient, order_id: str) -> tuple[float, str]:
    if not order_id:
        return 0.0, "NO_ORDER_ID"
    o = client.get_order(order_id)
    status = str(o.get("status") or "UNKNOWN")
    try:
        matched = float(o.get("size_matched") or 0.0)
    except Exception:
        matched = 0.0
    return matched, status


def _cancel_orders_safe(client: ClobClient, order_ids: list[str]):
    order_ids = [oid for oid in order_ids if oid]
    if not order_ids:
        return
    try:
        if len(order_ids) == 1:
            client.cancel(order_ids[0])
        else:
            client.cancel_orders(order_ids)
    except Exception as e:
        log.warning(f"Fallo cancel_orders: {e}")


def _get_rpc():
    for rpc in ["https://polygon-bor-rpc.publicnode.com", "https://polygon.meowrpc.com"]:
        try:
            w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 8}))
            if w3.is_connected():
                return w3
        except Exception:
            continue
    return None


def approve_ctf_for_selling():
    """
    Antes de vender condicionales, necesitamos que el Safe tenga aprobación ERC1155
    (setApprovalForAll) hacia los contratos que hacen transfer/sale en Polymarket.
    """
    PROXY = os.getenv("PROXY_ADDRESS")
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not PROXY or not pk:
        log.warning("CTF approval: falta PROXY_ADDRESS o POLYMARKET_PRIVATE_KEY; se omite.")
        return False

    # Direcciones usadas por el resto del repo (ej. contrarian_scalper.py)
    CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
    NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
    CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

    SAFE_ABI = [
        {
            "inputs": [
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "data", "type": "bytes"},
                {"name": "operation", "type": "uint8"},
                {"name": "safeTxGas", "type": "uint256"},
                {"name": "baseGas", "type": "uint256"},
                {"name": "gasPrice", "type": "uint256"},
                {"name": "gasToken", "type": "address"},
                {"name": "refundReceiver", "type": "address"},
                {"name": "signatures", "type": "bytes"},
            ],
            "name": "execTransaction",
            "outputs": [{"name": "success", "type": "bool"}],
            "type": "function",
        }
    ]

    ERC1155_APPROVAL_ABI = [
        {
            "inputs": [
                {"name": "account", "type": "address"},
                {"name": "operator", "type": "address"},
            ],
            "name": "isApprovedForAll",
            "outputs": [{"name": "", "type": "bool"}],
            "type": "function",
        },
        {
            "inputs": [
                {"name": "operator", "type": "address"},
                {"name": "approved", "type": "bool"},
            ],
            "name": "setApprovalForAll",
            "outputs": [],
            "type": "function",
        },
    ]

    w3 = _get_rpc()
    if not w3:
        log.warning("CTF approval: no RPC disponible; se omite.")
        return False

    account = Account.from_key(pk)
    proxy = Web3.to_checksum_address(PROXY)
    safe = w3.eth.contract(address=proxy, abi=SAFE_ABI)
    ctf = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=ERC1155_APPROVAL_ABI)

    operators = [CTF_EXCHANGE, NEG_RISK_EXCHANGE, NEG_RISK_ADAPTER]
    changed = False

    for op_addr in operators:
        op = Web3.to_checksum_address(op_addr)
        try:
            approved = ctf.functions.isApprovedForAll(proxy, op).call()
        except Exception:
            approved = None

        if approved:
            continue

        log.info(f"CTF approval: setApprovalForAll para operador {op_addr}...")
        try:
            data = ctf.encode_abi("setApprovalForAll", [op, True])
            # Signature placeholder estilo Safe (misma técnica que otros bots del repo)
            sig = (
                "0x000000000000000000000000"
                + account.address[2:].lower()
                + "0" * 64
                + "01"
            )
            tx = safe.functions.execTransaction(
                Web3.to_checksum_address(CTF_ADDRESS),
                0,
                data,
                0,  # operation = CALL
                0,
                0,
                0,
                Web3.to_checksum_address("0x0000000000000000000000000000000000000000"),
                Web3.to_checksum_address("0x0000000000000000000000000000000000000000"),
                Web3.to_bytes(hexstr=sig),
            ).build_transaction(
                {
                    "from": account.address,
                    "nonce": w3.eth.get_transaction_count(account.address, "pending"),
                    "gas": 150000,
                    "gasPrice": int(w3.eth.gas_price * 1.3),
                }
            )
            signed = w3.eth.account.sign_transaction(tx, private_key=pk)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            changed = True
        except Exception as e:
            log.warning(f"CTF approval: fallo para operador {op_addr}: {e}")

    return changed


def _available_conditional_balance(client: ClobClient, token_id: str) -> float:
    """
    Balance de conditional tokens en CLOB para el token_id dado.
    Se devuelve en "shares" (no en unidades base).
    """
    try:
        params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=str(token_id), signature_type=2)
        b_info = client.get_balance_allowance(params)
        bal = 0
        if isinstance(b_info, dict):
            bal = b_info.get("balance", 0)
        else:
            bal = getattr(b_info, "balance", 0)
        return float(bal) / 1e6
    except Exception as e:
        log.warning(f"balance conditional no disponible token={str(token_id)[:10]}...: {e}")
        return 0.0


def main():
    lock_fd = acquire_lock()
    traded = load_traded_markets()
    client = build_client()
    # Asegura que el Safe tenga aprobación para transferir condicionales al vender.
    try:
        approve_ctf_for_selling()
    except Exception as e:
        log.warning(f"CTF approval: fallo en startup (se intentará igual en caso de SELL): {e}")

    # Estado por mercado:
    # pending[mid] = {
    #   t0, first, ticker, token_yes, token_no,
    #   confirmed_at, yes_order_id, no_order_id,
    #   yes_filled, no_filled, yes_shares, no_shares
    # }
    pending = {}

    log.info(
        f"Straddle BOT (wallet={_env_file_path()}) | limit={LIMIT:.2f} other_within=+{OTHER_WITHIN:.2f} "
        f"confirm={CONFIRM_SEC:.0f}s timeout={TIMEOUT_SEC:.0f}s | usd_per_leg={USD_PER_LEG} | poll={POLL_SECONDS}s"
    )
    send_telegram(
        f"Arrancó. limit={LIMIT:.2f}, other_within=+{OTHER_WITHIN:.2f}, confirm={CONFIRM_SEC:.0f}s, timeout={TIMEOUT_SEC:.0f}s, usd/leg={USD_PER_LEG}, max_shares/leg={MAX_SHARES_PER_LEG}"
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
                    bid_yes, _ = best_prices(book_yes)
                    bid_no, _ = best_prices(book_no)
                    if ask_yes is None or ask_no is None:
                        continue

                    now_s = time.time()

                    # crear estado al primer cheap (NO colocar órdenes aún)
                    if market_id not in pending:
                        first = None
                        if ask_yes <= LIMIT:
                            first = "YES"
                        elif ask_no <= LIMIT:
                            first = "NO"
                        if not first:
                            continue
                        pending[market_id] = {
                            "t0": now_s,
                            "first": first,
                            "ticker": ticker,
                            "token_yes": token_yes,
                            "token_no": token_no,
                            "confirmed_at": None,
                            "yes_order_id": None,
                            "no_order_id": None,
                            "yes_filled": False,
                            "no_filled": False,
                            "yes_shares": None,
                            "no_shares": None,
                        }

                    st = pending.get(market_id)
                    if not st:
                        continue

                    # expirar confirmación
                    if now_s - st["t0"] > CONFIRM_SEC and st.get("confirmed_at") is None:
                        pending.pop(market_id, None)
                        continue

                    # confirmar "la otra pierna se acerca"
                    other_threshold = LIMIT + OTHER_WITHIN
                    # Confirmación: colocar 2ª pierna a LIMIT
                    if st.get("confirmed_at") is None:
                        if st["first"] == "YES":
                            if ask_no > other_threshold:
                                continue
                        else:
                            if ask_yes > other_threshold:
                                continue

                        # En confirmación: colocar ambas piernas a LIMIT
                        y_sh, _ = clamp_shares(USD_PER_LEG, LIMIT)
                        n_sh, _ = clamp_shares(USD_PER_LEG, LIMIT)
                        if y_sh <= 0 or n_sh <= 0:
                            pending.pop(market_id, None)
                            continue
                        try:
                            r_yes = safe_post(client, "BUY", token_yes, LIMIT, y_sh)
                            r_no = safe_post(client, "BUY", token_no, LIMIT, n_sh)
                        except Exception as e:
                            log.warning(f"Fallo post_order (2 piernas): {e}")
                            pending.pop(market_id, None)
                            continue

                        st["yes_order_id"] = (r_yes or {}).get("orderID")
                        st["no_order_id"] = (r_no or {}).get("orderID")
                        st["yes_shares"] = y_sh
                        st["no_shares"] = n_sh
                        st["confirmed_at"] = now_s
                        # Fill real se evalúa luego via get_order (evita “SELL sin balance”).
                        st["yes_filled"] = False
                        st["no_filled"] = False

                        q = (mkt.get("question") or "")[:90]
                        log.info(f"🟦 CONFIRM {ticker} mkt={market_id} | first={st['first']} | limit={LIMIT:.2f} | tleft={tleft:.0f}s | {q}")
                        send_telegram(f"CONFIRM {ticker} mkt_id={market_id} | placing YES+NO @ {LIMIT:.2f}")

                        log_trade({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "strategy": "doublecheap_straddle_filter_v3",
                            "result": "ENTRY_PLACED",
                            "market": ticker,
                            "market_id": market_id,
                            "question": mkt.get("question"),
                            "params": {"limit": LIMIT, "other_within": OTHER_WITHIN, "confirm_sec": CONFIRM_SEC, "timeout_sec": TIMEOUT_SEC, "usd_per_leg": USD_PER_LEG},
                            "orders": {
                                "yes": {"price": LIMIT, "shares": y_sh, "order_id": st["yes_order_id"]},
                                "no": {"price": LIMIT, "shares": n_sh, "order_id": st["no_order_id"]},
                            }
                        })

                    # Evaluar stop o completion (asumimos fill si ask<=LIMIT)
                    if st.get("confirmed_at") is not None:
                        # Reconciliar fills reales por order_id
                        try:
                            y_match, y_status = _order_matched_size(client, st.get("yes_order_id"))
                            n_match, n_status = _order_matched_size(client, st.get("no_order_id"))
                        except Exception as e:
                            log.warning(f"Fallo get_order: {e}")
                            continue

                        st["yes_filled"] = y_match >= 0.01
                        st["no_filled"] = n_match >= 0.01

                        # Si ambas piernas “llenadas” según heurística
                        if st.get("yes_filled") and st.get("no_filled"):
                            trade = {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "strategy": "doublecheap_straddle_filter_v3",
                                "result": "FILLED_2_LEGS",
                                "market": ticker,
                                "market_id": market_id,
                                "question": mkt.get("question"),
                                "params": {"limit": LIMIT, "other_within": OTHER_WITHIN, "confirm_sec": CONFIRM_SEC, "timeout_sec": TIMEOUT_SEC, "usd_per_leg": USD_PER_LEG},
                                "orders": {
                                    "yes_order_id": st.get("yes_order_id"),
                                    "no_order_id": st.get("no_order_id"),
                                    "yes_status": y_status,
                                    "no_status": n_status,
                                    "yes_matched": y_match,
                                    "no_matched": n_match,
                                },
                            }
                            log_trade(trade)
                            send_telegram(f"2 LEGS {ticker} mkt_id={market_id} | usd/leg={USD_PER_LEG}")
                            traded.add(market_id)
                            save_traded_markets(traded)
                            pending.pop(market_id, None)
                            time.sleep(10)
                            continue

                        # Stop si no completa en TIMEOUT_SEC
                        if now_s - float(st["confirmed_at"]) >= TIMEOUT_SEC:
                            to_cancel = []
                            if y_match < 0.01:
                                to_cancel.append(st.get("yes_order_id"))
                            if n_match < 0.01:
                                to_cancel.append(st.get("no_order_id"))
                            _cancel_orders_safe(client, to_cancel)

                            sells = []
                            attempted_ctf_approval = False
                            bid_yes2 = None
                            bid_no2 = None
                            try:
                                # refrescar bid actual para stop
                                book_yes2 = client.get_order_book(token_yes)
                                book_no2 = client.get_order_book(token_no)
                                bid_yes2, _ = best_prices(book_yes2)
                                bid_no2, _ = best_prices(book_no2)
                            except Exception as e:
                                log.warning(f"Fallo get bids para stop: {e}")

                            # SELL YES leg (con 1 retry si falla por allowance/balance)
                            if y_match >= 0.01 and bid_yes2 is not None:
                                r = None
                                sell_size = float(y_match)
                                try:
                                    r = safe_post(client, "SELL", token_yes, float(bid_yes2), sell_size)
                                except Exception as e:
                                    err = str(e).lower()
                                    log.warning(f"Fallo SELL stop YES: {e}")
                                    needs_approval = "not enough balance" in err and "allowance" in err
                                    if needs_approval:
                                        if not attempted_ctf_approval:
                                            approve_ctf_for_selling()
                                            attempted_ctf_approval = True
                                        # Puede haber delay entre el fill BUY y que el balance aparezca en CLOB.
                                        # Reintentamos algunas veces esperando balance.
                                        for _ in range(3):
                                            time.sleep(POLL_SECONDS)
                                            avail = _available_conditional_balance(client, token_yes)
                                            if avail > 0:
                                                sell_size = min(float(y_match), avail)
                                            else:
                                                sell_size = 0.0
                                            if sell_size < 0.01:
                                                continue
                                            try:
                                                r = safe_post(client, "SELL", token_yes, float(bid_yes2), sell_size)
                                                break
                                            except Exception as e2:
                                                log.warning(f"Retry SELL stop YES falló: {e2}")
                                                r = None
                                if r:
                                    sells.append({
                                        "side": "YES",
                                        "price": float(bid_yes2),
                                        "shares": sell_size,
                                        "order_id": (r or {}).get("orderID"),
                                    })

                            # SELL NO leg (con 1 retry si falla por allowance/balance)
                            if n_match >= 0.01 and bid_no2 is not None:
                                r = None
                                sell_size = float(n_match)
                                try:
                                    r = safe_post(client, "SELL", token_no, float(bid_no2), sell_size)
                                except Exception as e:
                                    err = str(e).lower()
                                    log.warning(f"Fallo SELL stop NO: {e}")
                                    needs_approval = "not enough balance" in err and "allowance" in err
                                    if needs_approval:
                                        if not attempted_ctf_approval:
                                            approve_ctf_for_selling()
                                            attempted_ctf_approval = True
                                        # Puede haber delay entre el fill BUY y que el balance aparezca en CLOB.
                                        # Reintentamos algunas veces esperando balance.
                                        for _ in range(3):
                                            time.sleep(POLL_SECONDS)
                                            avail = _available_conditional_balance(client, token_no)
                                            if avail > 0:
                                                sell_size = min(float(n_match), avail)
                                            else:
                                                sell_size = 0.0
                                            if sell_size < 0.01:
                                                continue
                                            try:
                                                r = safe_post(client, "SELL", token_no, float(bid_no2), sell_size)
                                                break
                                            except Exception as e2:
                                                log.warning(f"Retry SELL stop NO falló: {e2}")
                                                r = None
                                if r:
                                    sells.append({
                                        "side": "NO",
                                        "price": float(bid_no2),
                                        "shares": sell_size,
                                        "order_id": (r or {}).get("orderID"),
                                    })

                            trade = {
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "strategy": "doublecheap_straddle_filter_v3",
                                "result": "STOP_TIMEOUT",
                                "market": ticker,
                                "market_id": market_id,
                                "question": mkt.get("question"),
                                "params": {"limit": LIMIT, "other_within": OTHER_WITHIN, "confirm_sec": CONFIRM_SEC, "timeout_sec": TIMEOUT_SEC, "usd_per_leg": USD_PER_LEG},
                                "orders": {
                                    "yes_order_id": st.get("yes_order_id"),
                                    "no_order_id": st.get("no_order_id"),
                                    "yes_status": y_status,
                                    "no_status": n_status,
                                    "yes_matched": y_match,
                                    "no_matched": n_match,
                                },
                                "sells": sells,
                            }
                            log_trade(trade)
                            send_telegram(f"STOP {ticker} mkt_id={market_id} | timeout {TIMEOUT_SEC:.0f}s")
                            traded.add(market_id)
                            save_traded_markets(traded)
                            pending.pop(market_id, None)
                            time.sleep(10)
                            continue

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

