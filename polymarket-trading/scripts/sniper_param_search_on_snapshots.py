#!/usr/bin/env python3
"""
Búsqueda paramétrica para SNIPER sobre orderbook snapshots (enfoque BTC).

Pipeline:
1) Precalcula por snapshot: spot/vol/drift históricos, strike, p_yes (GBM estable), resolución.
2) Ejecuta grid-search rápido de reglas de entrada (min_edge, buffer, edades, ventanas, ask cap).
3) Devuelve ranking por score (PnL ajustado por drawdown y por varianza).
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import numpy as np
import requests

from straddle_optimizer import parse_jsonl, ts_to_num

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_TICKERS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


@dataclass
class SearchParams:
    min_edge: float
    edge_buffer: float
    min_market_age_sec: float
    time_left_min: float
    time_left_max: float
    ask_cap: float


def parse_float_list(s: str) -> list[float]:
    out = []
    for x in (s or "").split(","):
        x = x.strip()
        if not x:
            continue
        out.append(float(x))
    return out


def _gamma_event_start(market_id: str, cache: dict[str, str | None]) -> str | None:
    if not market_id:
        return None
    if market_id in cache:
        return cache[market_id]
    try:
        r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=8)
        if r.status_code != 200:
            cache[market_id] = None
            return None
        m = r.json()
        if not isinstance(m, dict):
            cache[market_id] = None
            return None
        cache[market_id] = m.get("eventStartTime") or m.get("startDate")
        return cache[market_id]
    except Exception:
        cache[market_id] = None
        return None


def _binance_kline_open(symbol: str, iso_utc: str, cache: dict[tuple[str, str], float | None]) -> float | None:
    k = (symbol, iso_utc)
    if k in cache:
        return cache[k]
    try:
        ts = int(datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).timestamp() * 1000)
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": ts, "limit": 1},
            timeout=5,
        )
        if not r.ok or not r.json():
            cache[k] = None
            return None
        v = float(r.json()[0][1])
        cache[k] = v
        return v
    except Exception:
        cache[k] = None
        return None


def _binance_close_at_ms(symbol: str, end_ms: int, cache: dict[tuple[str, int], float | None]) -> float | None:
    k = (symbol, int(end_ms))
    if k in cache:
        return cache[k]
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": int(end_ms), "limit": 1},
            timeout=5,
        )
        if not r.ok or not r.json():
            cache[k] = None
            return None
        v = float(r.json()[0][4])
        cache[k] = v
        return v
    except Exception:
        cache[k] = None
        return None


def _historic_spot_vol_drift(
    symbol: str, ts_unix: float, cache: dict[tuple[str, int], tuple[float | None, float | None, float | None]]
):
    k = (symbol, int(ts_unix // 60))
    if k in cache:
        return cache[k]
    ts_ms = int(ts_unix * 1000)
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "endTime": ts_ms, "limit": 62},
            timeout=6,
        )
        if not r.ok:
            cache[k] = (None, None, None)
            return cache[k]
        kl = r.json()
        if len(kl) < 12:
            cache[k] = (None, None, None)
            return cache[k]
        closes = np.array([float(x[4]) for x in kl[:-1]], dtype=float)[-60:]
        if len(closes) < 10:
            cache[k] = (None, None, None)
            return cache[k]
        spot = float(kl[-2][4])
        rets = np.diff(np.log(closes))
        vol = float(np.std(rets) * np.sqrt(525600))
        vol = max(0.10, min(vol, 2.50))
        drift = float(np.mean(rets) * 525600)
        drift = max(-1.0, min(drift, 1.0))
        cache[k] = (spot, vol, drift)
        return cache[k]
    except Exception:
        cache[k] = (None, None, None)
        return cache[k]


def _mc_prob_yes(s0, strike, tsec, vol, drift, simulations: int) -> float:
    if tsec <= 0:
        return 1.0 if s0 > strike else 0.0
    T = tsec / (365 * 24 * 60 * 60)
    steps = max(10, int(tsec / 5))
    steps = min(steps, 240)
    dt = T / steps
    rng = np.random.default_rng(12345)
    shocks = rng.normal(0, 1, (simulations, steps))
    paths = np.zeros((simulations, steps + 1), dtype=float)
    paths[:, 0] = s0
    for t in range(1, steps + 1):
        paths[:, t] = paths[:, t - 1] * np.exp((drift - 0.5 * vol**2) * dt + vol * np.sqrt(dt) * shocks[:, t - 1])
    return float(np.mean(paths[:, -1] > strike))


def _stable_prob_yes(s0, strike, tsec, vol, drift, simulations: int) -> float:
    p_d = _mc_prob_yes(s0, strike, tsec, vol, drift, simulations)
    p_0 = _mc_prob_yes(s0, strike, tsec, vol, 0.0, simulations)
    return 0.30 * p_d + 0.70 * p_0


def _fee_usd(notional: float, fee_bps: float) -> float:
    return notional * (fee_bps / 10000.0)


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


def build_candidates(
    in_file: str,
    ticker: str,
    stride: int,
    max_rows: int,
    mc_sims: int,
    one_per_market: bool,
    fast_no_gamma: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    symbol = BINANCE_TICKERS[ticker]
    stride = max(1, stride)

    event_cache: dict[str, str | None] = {}
    strike_cache: dict[tuple[str, str], float] = {}
    open_cache: dict[tuple[str, str], float | None] = {}
    spot_cache: dict[tuple[str, int], tuple[float | None, float | None, float | None]] = {}
    close_cache: dict[tuple[str, int], float | None] = {}
    used_markets: set[str] = set()

    skipped = {k: 0 for k in ("ticker", "time", "end", "market_dup", "binance", "strike", "asks", "resolve")}
    candidates: list[dict[str, Any]] = []

    rows_seen = 0
    for row in parse_jsonl(in_file):
        rows_seen += 1
        if max_rows > 0 and rows_seen > max_rows:
            break
        if (rows_seen - 1) % stride != 0:
            continue

        tk = (row.get("ticker") or "").upper()
        if tk != ticker:
            skipped["ticker"] += 1
            continue

        mid = str(row.get("market_id") or "")
        if one_per_market and mid in used_markets:
            skipped["market_dup"] += 1
            continue

        ts = ts_to_num(row.get("ts"))
        if ts is None:
            continue
        tl = row.get("time_left_s")
        try:
            tl = float(tl)
        except (TypeError, ValueError):
            skipped["time"] += 1
            continue

        end_s = row.get("endDate")
        if not end_s:
            skipped["end"] += 1
            continue
        try:
            end_dt = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
            end_ms = int(end_dt.timestamp() * 1000)
        except Exception:
            skipped["end"] += 1
            continue

        # spot/vol/drift históricos en ts snapshot
        spot, vol, drift = _historic_spot_vol_drift(symbol, float(ts), spot_cache)
        if spot is None or vol is None:
            skipped["binance"] += 1
            continue

        # strike por market start (Gamma -> Binance open). En modo fast usa strike=spot.
        if fast_no_gamma:
            start_iso = None
            strike = float(spot)
        else:
            k = (mid, ticker)
            if k in strike_cache:
                strike = strike_cache[k]
                start_iso = event_cache.get(mid)
            else:
                start_iso = _gamma_event_start(mid, event_cache)
                strike = 0.0
                if start_iso:
                    op = _binance_kline_open(symbol, start_iso, open_cache)
                    strike = float(op) if op is not None else 0.0
                strike_cache[k] = strike
            if strike <= 0:
                skipped["strike"] += 1
                continue

        # asks snapshot
        try:
            ask_yes = float(row.get("yes_ask"))
            ask_no = float(row.get("no_ask"))
        except (TypeError, ValueError):
            skipped["asks"] += 1
            continue

        px_end = _binance_close_at_ms(symbol, end_ms, close_cache)
        if px_end is None:
            skipped["resolve"] += 1
            continue

        age_sec = None
        if start_iso:
            try:
                age_sec = float(ts) - datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()
            except Exception:
                age_sec = None

        p_yes = _stable_prob_yes(spot, strike, tl, vol, drift, mc_sims)
        candidates.append(
            {
                "market_id": mid,
                "time_left": tl,
                "age_sec": age_sec,
                "ask_yes": ask_yes,
                "ask_no": ask_no,
                "p_yes": p_yes,
                "spot": spot,
                "strike": strike,
                "yes_won": px_end > strike,
                "no_won": px_end < strike,
            }
        )
        if one_per_market:
            used_markets.add(mid)

    meta = {
        "rows_seen": rows_seen,
        "candidates": len(candidates),
        "skipped": skipped,
        "cache_stats": {
            "event_cache": len(event_cache),
            "strike_cache": len(strike_cache),
            "spot_cache": len(spot_cache),
            "close_cache": len(close_cache),
        },
        "fast_no_gamma": bool(fast_no_gamma),
    }
    return candidates, meta


def eval_params(
    candidates: list[dict[str, Any]],
    p: SearchParams,
    trade_usd: float,
    max_shares: float,
    fee_bps: float,
) -> dict[str, Any]:
    pnls: list[float] = []
    wins = losses = 0
    entries_yes = entries_no = 0
    filtered = 0

    for c in candidates:
        tl = c["time_left"]
        if tl < p.time_left_min or tl > p.time_left_max:
            continue
        age = c.get("age_sec")
        if age is not None and age < p.min_market_age_sec:
            continue

        ask_y = c["ask_yes"]
        ask_n = c["ask_no"]
        p_yes = c["p_yes"]
        edge_yes = p_yes - ask_y - p.edge_buffer
        edge_no = (1.0 - p_yes) - ask_n - p.edge_buffer

        side = None
        entry = 0.0
        won = False
        if edge_yes > p.min_edge and ask_y < p.ask_cap:
            side = "YES"
            entry = ask_y
            won = bool(c["yes_won"])
            entries_yes += 1
        elif edge_no > p.min_edge and ask_n < p.ask_cap:
            side = "NO"
            entry = ask_n
            won = bool(c["no_won"])
            entries_no += 1
        else:
            filtered += 1
            continue

        shares = min(trade_usd / entry, max_shares)
        shares = max(1.0, round(shares, 2))
        notional = shares * entry
        gross = shares * 1.0 if won else 0.0
        pnl = gross - notional - _fee_usd(notional, fee_bps)
        pnls.append(pnl)
        if won:
            wins += 1
        else:
            losses += 1

    trades = len(pnls)
    pnl_total = float(sum(pnls))
    avg = pnl_total / trades if trades else 0.0
    win_rate = (wins / trades) if trades else 0.0
    max_dd = compute_max_drawdown(pnls) if pnls else 0.0
    std = float(np.std(pnls)) if pnls else 0.0
    # score: pnl castiga drawdown y varianza
    score = pnl_total - 0.35 * max_dd - 0.10 * std * max(1.0, np.sqrt(trades))

    return {
        "min_edge": p.min_edge,
        "edge_buffer": p.edge_buffer,
        "min_market_age_sec": p.min_market_age_sec,
        "time_left_min": p.time_left_min,
        "time_left_max": p.time_left_max,
        "ask_cap": p.ask_cap,
        "trades": trades,
        "entries_yes": entries_yes,
        "entries_no": entries_no,
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "pnl_total": pnl_total,
        "pnl_avg": avg,
        "max_drawdown": max_dd,
        "std_pnl": std,
        "score": score,
        "filtered_no_trade": filtered,
    }


def run_grid(
    candidates: list[dict[str, Any]],
    min_edge_values: list[float],
    edge_buffer_values: list[float],
    min_age_values: list[float],
    tl_min_values: list[float],
    tl_max_values: list[float],
    ask_cap_values: list[float],
    trade_usd: float,
    max_shares: float,
    fee_bps: float,
    min_trades: int,
    max_dd_limit: float | None,
    top_k: int,
) -> dict[str, Any]:
    tested = 0
    rows = []
    for min_edge in min_edge_values:
        for edge_buffer in edge_buffer_values:
            for min_age in min_age_values:
                for tl_min in tl_min_values:
                    for tl_max in tl_max_values:
                        if tl_min >= tl_max:
                            continue
                        for ask_cap in ask_cap_values:
                            tested += 1
                            st = eval_params(
                                candidates,
                                SearchParams(min_edge, edge_buffer, min_age, tl_min, tl_max, ask_cap),
                                trade_usd=trade_usd,
                                max_shares=max_shares,
                                fee_bps=fee_bps,
                            )
                            if st["trades"] < min_trades:
                                continue
                            if max_dd_limit is not None and st["max_drawdown"] > max_dd_limit:
                                continue
                            rows.append(st)

    rows.sort(key=lambda r: (r["score"], r["pnl_total"], r["win_rate"]), reverse=True)
    return {"tested": tested, "kept": len(rows), "top": rows[:top_k]}


def main():
    ap = argparse.ArgumentParser(description="Optimización paramétrica SNIPER BTC sobre snapshots.")
    ap.add_argument("--in-file", default=os.path.expanduser(os.getenv("OB_IN_FILE", "~/orderbook_snapshots.jsonl")))
    ap.add_argument("--ticker", default="BTC", choices=["BTC", "ETH"])
    ap.add_argument("--stride", type=int, default=12)
    ap.add_argument("--max-rows", type=int, default=0)
    ap.add_argument("--mc-sims", type=int, default=500)
    ap.add_argument("--one-per-market", action="store_true", default=True)
    ap.add_argument("--fast-no-gamma", action="store_true", help="Acelera usando strike=spot y sin filtro de edad.")
    ap.add_argument("--min-edge-values", default="0.03,0.05,0.08,0.10,0.12")
    ap.add_argument("--edge-buffer-values", default="0.00,0.01,0.02,0.03,0.04")
    ap.add_argument("--min-age-values", default="0,60,120,180")
    ap.add_argument("--tl-min-values", default="60,120,180")
    ap.add_argument("--tl-max-values", default="600,900,1200")
    ap.add_argument("--ask-cap-values", default="0.75,0.80,0.85,0.90")
    ap.add_argument("--trade-usd", type=float, default=2.0)
    ap.add_argument("--max-shares", type=float, default=4.0)
    ap.add_argument("--fee-bps", type=float, default=25.0)
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--max-dd-limit", type=float, default=-1.0, help="<0 desactiva filtro.")
    ap.add_argument("--top-k", type=int, default=20)
    ap.add_argument("--out-json", default="")
    args = ap.parse_args()

    path = os.path.expanduser(args.in_file)
    if not os.path.isfile(path):
        raise SystemExit(f"No existe input file: {path}")

    candidates, meta = build_candidates(
        in_file=path,
        ticker=args.ticker,
        stride=args.stride,
        max_rows=args.max_rows,
        mc_sims=max(200, min(args.mc_sims, 3000)),
        one_per_market=bool(args.one_per_market),
        fast_no_gamma=bool(args.fast_no_gamma),
    )

    out = run_grid(
        candidates=candidates,
        min_edge_values=parse_float_list(args.min_edge_values),
        edge_buffer_values=parse_float_list(args.edge_buffer_values),
        min_age_values=parse_float_list(args.min_age_values),
        tl_min_values=parse_float_list(args.tl_min_values),
        tl_max_values=parse_float_list(args.tl_max_values),
        ask_cap_values=parse_float_list(args.ask_cap_values),
        trade_usd=args.trade_usd,
        max_shares=args.max_shares,
        fee_bps=args.fee_bps,
        min_trades=args.min_trades,
        max_dd_limit=(None if args.max_dd_limit < 0 else args.max_dd_limit),
        top_k=args.top_k,
    )

    result = {
        "in_file": path,
        "ticker": args.ticker,
        "build_meta": meta,
        "search": {
            "tested": out["tested"],
            "kept": out["kept"],
            "top": out["top"],
        },
    }
    if args.out_json:
        with open(args.out_json, "w") as f:
            json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

