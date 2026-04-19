import argparse
import csv
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass
class SimParams:
    timeout_sec: float
    usd_per_leg: float
    limit_price: float
    other_within: float
    confirm_sec: float
    max_shares_per_leg: float = 6.0
    fee_enabled: bool = True
    fee_exponent: int = 2
    fee_min_usdc: float = 0.0001


def ts_to_num(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        try:
            return float(s)
        except Exception:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None
    return None


def parse_jsonl(path: str):
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def clamp_shares(usd_per_leg: float, limit_price: float, max_shares_per_leg: float) -> float:
    if limit_price <= 0:
        return 0.0
    shares = round(usd_per_leg / limit_price, 6)
    shares = min(shares, max_shares_per_leg)
    return max(0.0, shares)


def fetch_fee_rate_bps(token_id: str, cache: dict[str, int]) -> int:
    if not token_id:
        return 0
    if token_id in cache:
        return cache[token_id]
    try:
        r = requests.get(f"{CLOB_API}/fee-rate", params={"token_id": token_id}, timeout=5)
        if not r.ok:
            cache[token_id] = 0
            return 0
        j = r.json() or {}
        bps = int(j.get("base_fee") or 0)
        cache[token_id] = bps
        return bps
    except Exception:
        cache[token_id] = 0
        return 0


def fee_usdc(shares: float, price: float, base_fee_bps: int, p: SimParams) -> float:
    if not p.fee_enabled or base_fee_bps <= 0:
        return 0.0
    if shares <= 0 or price <= 0 or price >= 1:
        return 0.0
    fee_rate = base_fee_bps / 10000.0
    raw = shares * price * fee_rate * ((price * (1.0 - price)) ** p.fee_exponent)
    f = round(raw, 4)
    if 0 < f < p.fee_min_usdc:
        f = p.fee_min_usdc
    return f


def first_touch_within(rows_ask: list[tuple[float, float]], threshold: float, t0: float, t1: float):
    for ts, ask in rows_ask:
        if ts < t0:
            continue
        if ts > t1:
            break
        if isinstance(ask, (int, float)) and ask <= threshold:
            return ts, ask
    return None


def first_bid_at_or_after(rows_bid: list[tuple[float, float]], ts_target: float):
    for ts, bid in rows_bid:
        if ts < ts_target:
            continue
        if isinstance(bid, (int, float)):
            return ts, bid
    return None


def compute_max_drawdown(pnls: list[float]) -> float:
    eq = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        dd = peak - eq
        max_dd = max(max_dd, dd)
    return max_dd


def load_markets(in_file: str) -> tuple[list[dict[str, Any]], int]:
    per: dict[str, dict[str, Any]] = {}
    n_lines = 0
    for o in parse_jsonl(in_file):
        n_lines += 1
        mid = str(o.get("market_id"))
        ts = ts_to_num(o.get("ts"))
        if ts is None:
            continue
        ya = o.get("yes_ask")
        na = o.get("no_ask")
        yb = o.get("yes_bid")
        nb = o.get("no_bid")
        if mid not in per:
            per[mid] = {
                "market_id": mid,
                "ticker": (o.get("ticker") or "").upper(),
                "question": o.get("question"),
                "token_yes": o.get("token_yes"),
                "token_no": o.get("token_no"),
                "rows": [],
            }
        per[mid]["rows"].append((ts, ya, na, yb, nb))

    markets = list(per.values())
    for m in markets:
        rows_sorted = sorted(m["rows"], key=lambda r: r[0])
        m["yes_ask"] = [(ts, ya) for ts, ya, _, _, _ in rows_sorted if isinstance(ya, (int, float))]
        m["no_ask"] = [(ts, na) for ts, _, na, _, _ in rows_sorted if isinstance(na, (int, float))]
        m["yes_bid"] = [(ts, yb) for ts, _, _, yb, _ in rows_sorted if isinstance(yb, (int, float))]
        m["no_bid"] = [(ts, nb) for ts, _, _, _, nb in rows_sorted if isinstance(nb, (int, float))]
    return markets, n_lines


def simulate_option2(markets: list[dict[str, Any]], p: SimParams, fee_cache: dict[str, int] | None = None):
    if fee_cache is None:
        fee_cache = {}

    stats = {
        "triggers": 0,
        "conv_2legs": 0,
        "stops": 0,
        "no_fill": 0,
        "pnl_total": 0.0,
        "pnl_conv": 0.0,
        "pnl_stop": 0.0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "trade_pnls": [],
        "by_ticker": {
            "BTC": {"triggers": 0, "conv": 0, "stops": 0, "pnl": 0.0},
            "ETH": {"triggers": 0, "conv": 0, "stops": 0, "pnl": 0.0},
        },
    }

    shares_per_leg = clamp_shares(p.usd_per_leg, p.limit_price, p.max_shares_per_leg)
    if shares_per_leg <= 0:
        return stats

    for m in markets:
        ticker = m.get("ticker") if m.get("ticker") in ("BTC", "ETH") else "OTHER"
        yes_rows = m["yes_ask"]
        no_rows = m["no_ask"]
        yes_bids = m["yes_bid"]
        no_bids = m["no_bid"]
        tok_yes = m.get("token_yes") or ""
        tok_no = m.get("token_no") or ""

        first_yes = first_touch_within(yes_rows, p.limit_price, -1e18, 1e18)
        first_no = first_touch_within(no_rows, p.limit_price, -1e18, 1e18)
        if not first_yes and not first_no:
            continue

        if first_yes and (not first_no or first_yes[0] <= first_no[0]):
            first_side = "YES"
            t0 = first_yes[0]
            confirm = first_touch_within(no_rows, p.limit_price + p.other_within, t0, t0 + p.confirm_sec)
            if not confirm:
                continue
            t_entry = confirm[0]
        else:
            first_side = "NO"
            t0 = first_no[0]
            confirm = first_touch_within(yes_rows, p.limit_price + p.other_within, t0, t0 + p.confirm_sec)
            if not confirm:
                continue
            t_entry = confirm[0]

        stats["triggers"] += 1
        if ticker in ("BTC", "ETH"):
            stats["by_ticker"][ticker]["triggers"] += 1

        t_deadline = t_entry + p.timeout_sec
        yes_fill = first_touch_within(yes_rows, p.limit_price, t_entry, t_deadline)
        no_fill = first_touch_within(no_rows, p.limit_price, t_entry, t_deadline)

        fee_buy_yes = fee_usdc(shares_per_leg, p.limit_price, fetch_fee_rate_bps(tok_yes, fee_cache), p)
        fee_buy_no = fee_usdc(shares_per_leg, p.limit_price, fetch_fee_rate_bps(tok_no, fee_cache), p)
        entry_cost = shares_per_leg * p.limit_price

        if yes_fill and no_fill:
            stats["conv_2legs"] += 1
            if ticker in ("BTC", "ETH"):
                stats["by_ticker"][ticker]["conv"] += 1

            # Straddle payoff aprox: una pierna paga 1, la otra 0 => ingreso shares_per_leg.
            pnl = shares_per_leg - (2 * entry_cost) - (fee_buy_yes + fee_buy_no)
            stats["pnl_conv"] += pnl
            stats["pnl_total"] += pnl
            stats["trade_pnls"].append(pnl)
            if ticker in ("BTC", "ETH"):
                stats["by_ticker"][ticker]["pnl"] += pnl
            if pnl > 1e-9:
                stats["wins"] += 1
            elif pnl < -1e-9:
                stats["losses"] += 1
            else:
                stats["breakeven"] += 1
            continue

        # Stop timeout (pierna simple)
        matched_side = None
        tok = ""
        bid_rows = None
        if yes_fill and not no_fill:
            matched_side = "YES"
            tok = tok_yes
            bid_rows = yes_bids
            fee_buy = fee_buy_yes
        elif no_fill and not yes_fill:
            matched_side = "NO"
            tok = tok_no
            bid_rows = no_bids
            fee_buy = fee_buy_no
        else:
            # ninguna pierna llenó en timeout
            stats["no_fill"] += 1
            continue

        stats["stops"] += 1
        if ticker in ("BTC", "ETH"):
            stats["by_ticker"][ticker]["stops"] += 1

        bid = first_bid_at_or_after(bid_rows, t_deadline) if bid_rows else None
        if not bid:
            # Caso conservador: si no hay bid, pérdida completa de esa pierna llenada.
            pnl = -(entry_cost + fee_buy)
        else:
            sell_px = float(bid[1])
            fee_sell = fee_usdc(shares_per_leg, sell_px, fetch_fee_rate_bps(tok, fee_cache), p)
            pnl = shares_per_leg * sell_px - entry_cost - fee_buy - fee_sell

        stats["pnl_stop"] += pnl
        stats["pnl_total"] += pnl
        stats["trade_pnls"].append(pnl)
        if ticker in ("BTC", "ETH"):
            stats["by_ticker"][ticker]["pnl"] += pnl
        if pnl > 1e-9:
            stats["wins"] += 1
        elif pnl < -1e-9:
            stats["losses"] += 1
        else:
            stats["breakeven"] += 1

    stats["max_drawdown"] = compute_max_drawdown(stats["trade_pnls"])
    stats["trades"] = stats["conv_2legs"] + stats["stops"]
    stats["conv_rate"] = (stats["conv_2legs"] / stats["triggers"]) if stats["triggers"] else 0.0
    stats["stop_rate"] = (stats["stops"] / stats["triggers"]) if stats["triggers"] else 0.0
    return stats


def parse_float_list(s: str) -> list[float]:
    out = []
    for x in s.split(","):
        x = x.strip()
        if not x:
            continue
        out.append(float(x))
    return out


def run_grid_search(
    markets: list[dict[str, Any]],
    timeout_values: list[float],
    usd_values: list[float],
    limit_values: list[float],
    other_within_values: list[float],
    confirm_values: list[float],
    min_trades: int,
    max_drawdown_limit: float | None,
    top_k: int,
    max_shares_per_leg: float = 6.0,
):
    fee_cache: dict[str, int] = {}
    results = []
    tested = 0

    for timeout_sec in timeout_values:
        for usd_per_leg in usd_values:
            for limit_price in limit_values:
                for other_within in other_within_values:
                    for confirm_sec in confirm_values:
                        tested += 1
                        params = SimParams(
                            timeout_sec=timeout_sec,
                            usd_per_leg=usd_per_leg,
                            limit_price=limit_price,
                            other_within=other_within,
                            confirm_sec=confirm_sec,
                            max_shares_per_leg=max_shares_per_leg,
                        )
                        st = simulate_option2(markets, params, fee_cache=fee_cache)
                        if st["trades"] < min_trades:
                            continue
                        if max_drawdown_limit is not None and st["max_drawdown"] > max_drawdown_limit:
                            continue

                        pnl_btc = st["by_ticker"]["BTC"]["pnl"]
                        pnl_eth = st["by_ticker"]["ETH"]["pnl"]
                        # score de estabilidad: penaliza fuerte cuando un activo va negativo.
                        stability_penalty = 0.0
                        if pnl_btc < 0:
                            stability_penalty += abs(pnl_btc) * 0.5
                        if pnl_eth < 0:
                            stability_penalty += abs(pnl_eth) * 0.5
                        score = st["pnl_total"] - stability_penalty

                        results.append(
                            {
                                "timeout_sec": timeout_sec,
                                "usd_per_leg": usd_per_leg,
                                "limit_price": limit_price,
                                "other_within": other_within,
                                "confirm_sec": confirm_sec,
                                "triggers": st["triggers"],
                                "trades": st["trades"],
                                "conv_2legs": st["conv_2legs"],
                                "stops": st["stops"],
                                "conv_rate": st["conv_rate"],
                                "stop_rate": st["stop_rate"],
                                "wins": st["wins"],
                                "losses": st["losses"],
                                "breakeven": st["breakeven"],
                                "pnl_total": st["pnl_total"],
                                "pnl_conv": st["pnl_conv"],
                                "pnl_stop": st["pnl_stop"],
                                "pnl_btc": pnl_btc,
                                "pnl_eth": pnl_eth,
                                "max_drawdown": st["max_drawdown"],
                                "score": score,
                            }
                        )

    results.sort(key=lambda r: (r["score"], r["pnl_total"]), reverse=True)
    return {
        "tested": tested,
        "kept": len(results),
        "top": results[:top_k],
    }


def save_csv(path: str, rows: list[dict[str, Any]]):
    if not rows:
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def cli():
    ap = argparse.ArgumentParser(description="Optimización de parámetros para straddle opción 2.")
    ap.add_argument("--in-file", default=os.path.expanduser(os.getenv("OB_IN_FILE", "~/orderbook_snapshots.jsonl")))
    ap.add_argument("--timeout-values", default="20,30,45,60,90")
    ap.add_argument("--usd-values", default="1,2,3,4")
    ap.add_argument("--limit-values", default="0.30,0.32,0.35")
    ap.add_argument("--other-within-values", default="0.01,0.02,0.03")
    ap.add_argument("--confirm-values", default="30,45,60,90")
    ap.add_argument("--min-trades", type=int, default=30)
    ap.add_argument("--max-drawdown-limit", type=float, default=-1.0, help="Si <0, no aplica filtro.")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--max-shares-per-leg", type=float, default=6.0)
    ap.add_argument("--out-json", default="")
    ap.add_argument("--out-csv", default="")
    args = ap.parse_args()

    markets, n_lines = load_markets(args.in_file)
    res = run_grid_search(
        markets=markets,
        timeout_values=parse_float_list(args.timeout_values),
        usd_values=parse_float_list(args.usd_values),
        limit_values=parse_float_list(args.limit_values),
        other_within_values=parse_float_list(args.other_within_values),
        confirm_values=parse_float_list(args.confirm_values),
        min_trades=args.min_trades,
        max_drawdown_limit=(None if args.max_drawdown_limit < 0 else args.max_drawdown_limit),
        top_k=args.top_k,
        max_shares_per_leg=args.max_shares_per_leg,
    )

    out = {
        "in_file": args.in_file,
        "lines_read": n_lines,
        "markets": len(markets),
        "tested_combinations": res["tested"],
        "kept_combinations": res["kept"],
        "top": res["top"],
    }

    if args.out_json:
        with open(args.out_json, "w") as f:
            json.dump(out, f, indent=2)
    if args.out_csv:
        save_csv(args.out_csv, res["top"])

    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    cli()
