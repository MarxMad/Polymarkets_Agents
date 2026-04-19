#!/usr/bin/env python3
"""
Puente Straddle: snapshots JSONL + Binance → features, surrogate, validación y simulación larga.

Subcomandos: join | describe | train | validate | simulate-binance

Ver docs/STRADDLE_SNAPSHOT_BINANCE_BRIDGE.md (Fase 0 y uso).
"""

from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import requests

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from straddle_optimizer import SimParams, parse_jsonl, simulate_option2, ts_to_num  # noqa: E402

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE = "https://api.binance.com/api/v3/klines"
SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


def _gamma_event_start(market_id: str, cache: dict[str, str | None]) -> str | None:
    if not market_id:
        return None
    if market_id in cache:
        return cache[market_id]
    try:
        r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=10)
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


def _binance_open_at_iso(symbol: str, iso_utc: str, cache: dict[tuple[str, str], float | None]) -> float | None:
    k = (symbol, iso_utc)
    if k in cache:
        return cache[k]
    try:
        ts = int(datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).timestamp() * 1000)
        r = requests.get(BINANCE, params={"symbol": symbol, "interval": "1m", "startTime": ts, "limit": 1}, timeout=8)
        if not r.ok or not r.json():
            cache[k] = None
            return None
        v = float(r.json()[0][1])
        cache[k] = v
        return v
    except Exception:
        cache[k] = None
        return None


def _download_klines_1m(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float, float]]:
    """Lista de (open_time_ms, open, close) ordenada."""
    out: list[tuple[int, float, float]] = []
    cur = int(start_ms)
    end_ms = int(end_ms)
    while cur <= end_ms:
        try:
            r = requests.get(
                BINANCE,
                params={"symbol": symbol, "interval": "1m", "startTime": cur, "limit": 1000},
                timeout=15,
            )
            if not r.ok:
                break
            kl = r.json()
            if not kl:
                break
            for row in kl:
                ot = int(row[0])
                o, c = float(row[1]), float(row[4])
                out.append((ot, o, c))
            cur = int(kl[-1][0]) + 60_000
            time.sleep(0.05)
        except Exception:
            break
    out.sort(key=lambda x: x[0])
    # dedupe by open time
    dedup: dict[int, tuple[int, float, float]] = {}
    for t in out:
        dedup[t[0]] = t
    return [dedup[k] for k in sorted(dedup)]


def _build_close_lookup(klines: list[tuple[int, float, float]]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """open_ms_sorted, open, close."""
    if not klines:
        z = np.array([], dtype=np.int64)
        f = np.array([], dtype=float)
        return z, f, f
    oms = np.array([k[0] for k in klines], dtype=np.int64)
    opens = np.array([k[1] for k in klines], dtype=float)
    closes = np.array([k[2] for k in klines], dtype=float)
    return oms, opens, closes


def _close_at_snapshot(open_ms_arr: np.ndarray, closes: np.ndarray, ts_sec: float) -> float | None:
    if open_ms_arr.size == 0:
        return None
    want = int(int(ts_sec) // 60 * 60 * 1000)
    i = bisect.bisect_right(open_ms_arr, want) - 1
    if i < 0:
        return None
    if open_ms_arr[i] != want:
        return None
    return float(closes[i])


def _vol_15m(open_ms_arr: np.ndarray, closes: np.ndarray, ts_sec: float) -> float:
    """Std de retornos log de hasta 15 velas 1m anteriores al minuto del snapshot; escala sqrt(525600), clamp."""
    want = int(int(ts_sec) // 60 * 60 * 1000)
    i = bisect.bisect_right(open_ms_arr, want) - 1
    if i < 1:
        return 0.05
    lo = max(0, i - 15)
    seg = closes[lo:i]
    if seg.size < 3:
        return 0.05
    lr = np.diff(np.log(np.clip(seg, 1e-12, None)))
    v = float(np.std(lr) * math.sqrt(525600.0))
    return max(0.05, min(v, 3.0))


def _time_features(ts_sec: float) -> tuple[float, float, float, float]:
    dt = datetime.fromtimestamp(ts_sec, tz=timezone.utc)
    h = dt.hour + dt.minute / 60.0
    dow = dt.weekday()
    return (
        math.sin(2 * math.pi * h / 24.0),
        math.cos(2 * math.pi * h / 24.0),
        math.sin(2 * math.pi * dow / 7.0),
        math.cos(2 * math.pi * dow / 7.0),
    )


def _scan_bounds_and_markets(
    snapshots_path: str, stride: int, max_rows: int
) -> tuple[dict[str, dict[str, Any]], float | None, float | None, float | None, float | None, int]:
    """market_id -> {ticker, first_ts, last_ts, endDate sample}; mins per symbol."""
    per: dict[str, dict[str, Any]] = {}
    btc_min = btc_max = None
    eth_min = eth_max = None
    seen = 0
    for o in parse_jsonl(snapshots_path):
        seen += 1
        if max_rows > 0 and seen > max_rows:
            break
        if (seen - 1) % stride != 0:
            continue
        mid = str(o.get("market_id") or "")
        if not mid:
            continue
        ts = ts_to_num(o.get("ts"))
        if ts is None:
            continue
        tk = (o.get("ticker") or "").upper()
        if tk not in ("BTC", "ETH"):
            continue
        if mid not in per:
            per[mid] = {"ticker": tk, "min_ts": ts, "max_ts": ts, "endDate": o.get("endDate")}
        else:
            per[mid]["min_ts"] = min(per[mid]["min_ts"], ts)
            per[mid]["max_ts"] = max(per[mid]["max_ts"], ts)
        if tk == "BTC":
            btc_min = ts if btc_min is None else min(btc_min, ts)
            btc_max = ts if btc_max is None else max(btc_max, ts)
        else:
            eth_min = ts if eth_min is None else min(eth_min, ts)
            eth_max = ts if eth_max is None else max(eth_max, ts)
    return per, btc_min, btc_max, eth_min, eth_max, seen


def _resolve_strikes(
    per: dict[str, dict[str, Any]], gamma_cache: dict[str, str | None], open_cache: dict[tuple[str, str], float | None]
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for mid, info in per.items():
        sym = SYMBOLS.get(info["ticker"])
        if not sym:
            continue
        start_iso = _gamma_event_start(mid, gamma_cache)
        if not start_iso:
            continue
        strike = _binance_open_at_iso(sym, start_iso, open_cache)
        if strike is None or strike <= 0:
            continue
        try:
            end_dt = datetime.fromisoformat(str(info.get("endDate") or "").replace("Z", "+00:00"))
            end_ts = end_dt.timestamp()
        except Exception:
            continue
        try:
            start_ts = datetime.fromisoformat(start_iso.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        out[mid] = {
            "ticker": info["ticker"],
            "strike": float(strike),
            "event_start_ts": start_ts,
            "end_ts": float(end_ts),
            "start_iso": start_iso,
        }
    return out


def cmd_join(args: argparse.Namespace) -> None:
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path = out_dir / "bridge_market_meta.json"
    feat_path = out_dir / "features.jsonl.gz"

    per, btc_min, btc_max, eth_min, eth_max, rows_seen = _scan_bounds_and_markets(
        args.snapshots, max(1, int(args.stride)), int(args.max_rows)
    )
    gamma_cache: dict[str, str | None] = {}
    open_cache: dict[tuple[str, str], float | None] = {}
    strikes = _resolve_strikes(per, gamma_cache, open_cache)
    meta_path.write_text(
        json.dumps(
            {
                "rows_seen_scan": rows_seen,
                "markets_unique": len(per),
                "markets_with_strike": len(strikes),
                "strikes": strikes,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    pad_ms = 3_600_000
    kl_btc: list[tuple[int, float, float]] = []
    kl_eth: list[tuple[int, float, float]] = []
    if btc_min is not None and btc_max is not None:
        s_ms = int(btc_min * 1000) - pad_ms
        e_ms = int(btc_max * 1000) + pad_ms
        print("Downloading BTC 1m klines …")
        kl_btc = _download_klines_1m("BTCUSDT", s_ms, e_ms)
    if eth_min is not None and eth_max is not None:
        s_ms = int(eth_min * 1000) - pad_ms
        e_ms = int(eth_max * 1000) + pad_ms
        print("Downloading ETH 1m klines …")
        kl_eth = _download_klines_1m("ETHUSDT", s_ms, e_ms)

    o_btc, _, c_btc = _build_close_lookup(kl_btc)
    o_eth, _, c_eth = _build_close_lookup(kl_eth)

    n_written = 0
    n_skip = defaultdict(int)
    t0 = time.time()
    with gzip.open(feat_path, "wt", encoding="utf-8") as gz:
        seen = 0
        for o in parse_jsonl(args.snapshots):
            seen += 1
            if args.max_rows > 0 and seen > args.max_rows:
                break
            if (seen - 1) % max(1, int(args.stride)) != 0:
                continue
            mid = str(o.get("market_id") or "")
            m = strikes.get(mid)
            if not m:
                n_skip["no_strike"] += 1
                continue
            ts = ts_to_num(o.get("ts"))
            if ts is None:
                n_skip["no_ts"] += 1
                continue
            try:
                ya = float(o.get("yes_ask"))
                na = float(o.get("no_ask"))
            except (TypeError, ValueError):
                n_skip["no_asks"] += 1
                continue
            if not (0 < ya < 1 and 0 < na < 1):
                n_skip["asks_oob"] += 1
                continue
            try:
                tl = float(o.get("time_left_s"))
            except (TypeError, ValueError):
                n_skip["no_tl"] += 1
                continue

            sym_key = m["ticker"]
            oms, closes = (o_btc, c_btc) if sym_key == "BTC" else (o_eth, c_eth)
            spot = _close_at_snapshot(oms, closes, float(ts))
            if spot is None or spot <= 0:
                n_skip["no_spot"] += 1
                continue
            strike = float(m["strike"])
            log_m = math.log(spot / strike)
            tl_norm = max(0.0, min(2.0, tl / 300.0))
            vol = _vol_15m(oms, closes, float(ts))
            hs, hc, ds, dc = _time_features(float(ts))
            is_btc = 1.0 if sym_key == "BTC" else 0.0

            row = {
                "ts": float(ts),
                "market_id": mid,
                "ticker": sym_key,
                "time_left_s": tl,
                "yes_ask": ya,
                "no_ask": na,
                "strike": strike,
                "spot": spot,
                "log_m": log_m,
                "tl_norm": tl_norm,
                "tl_norm2": tl_norm**2,
                "vol_15m": vol,
                "is_btc": is_btc,
                "hour_sin": hs,
                "hour_cos": hc,
                "dow_sin": ds,
                "dow_cos": dc,
            }
            gz.write(json.dumps(row, separators=(",", ":")) + "\n")
            n_written += 1
            if n_written % 200_000 == 0:
                print(f"  … {n_written} rows ({time.time() - t0:.1f}s)")

    print(
        json.dumps(
            {
                "out": str(feat_path),
                "meta": str(meta_path),
                "rows_written": n_written,
                "skipped": dict(n_skip),
                "seconds": round(time.time() - t0, 2),
            },
            indent=2,
        )
    )


def _iter_features(path: str, max_lines: int = 0) -> Iterator[dict[str, Any]]:
    opener = gzip.open if path.endswith(".gz") else open
    mode = "rt" if path.endswith(".gz") else "r"
    encoding = "utf-8"
    n = 0
    with opener(path, mode, encoding=encoding) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)
            n += 1
            if max_lines and n >= max_lines:
                break


FEATURE_KEYS = [
    "log_m",
    "tl_norm",
    "tl_norm2",
    "vol_15m",
    "is_btc",
    "hour_sin",
    "hour_cos",
    "dow_sin",
    "dow_cos",
]


def _load_xy(
    features_path: str, max_lines: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[float]]:
    xs: list[list[float]] = []
    ys_y: list[float] = []
    ys_n: list[float] = []
    ts_list: list[float] = []
    for row in _iter_features(features_path, max_lines or 0):
        xs.append([row[k] for k in FEATURE_KEYS])
        ys_y.append(row["yes_ask"])
        ys_n.append(row["no_ask"])
        ts_list.append(float(row["ts"]))
    if not xs:
        raise SystemExit("Sin filas en features.")
    return np.array(xs, dtype=np.float64), np.array(ys_y), np.array(ys_n), ts_list


def cmd_train(args: argparse.Namespace) -> None:
    from sklearn.ensemble import RandomForestRegressor
    import joblib

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    X, yy, yn, ts_list = _load_xy(args.features, int(args.max_lines or 0))
    ts_arr = np.array(ts_list)
    order = np.argsort(ts_arr)
    X, yy, yn = X[order], yy[order], yn[order]
    n = X.shape[0]
    cut = int(n * float(args.train_frac))
    Xtr, Xte = X[:cut], X[cut:]
    yy_tr, yy_te = yy[:cut], yy[cut:]
    yn_tr, yn_te = yn[:cut], yn[cut:]

    rf_y = RandomForestRegressor(
        n_estimators=int(args.n_trees),
        max_depth=int(args.max_depth),
        min_samples_leaf=int(args.min_samples_leaf),
        random_state=42,
        n_jobs=-1,
    )
    rf_n = RandomForestRegressor(
        n_estimators=int(args.n_trees),
        max_depth=int(args.max_depth),
        min_samples_leaf=int(args.min_samples_leaf),
        random_state=43,
        n_jobs=-1,
    )
    rf_y.fit(Xtr, yy_tr)
    rf_n.fit(Xtr, yn_tr)

    joblib.dump(
        {
            "feature_names": FEATURE_KEYS,
            "rf_yes": rf_y,
            "rf_no": rf_n,
            "train_frac": float(args.train_frac),
            "n_train": int(cut),
            "n_test": int(n - cut),
        },
        out_dir / "surrogate.joblib",
    )
    print(json.dumps({"saved": str(out_dir / "surrogate.joblib"), "n_train": cut, "n_test": n - cut}, indent=2))


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    e = y_true - y_pred
    mae = float(np.mean(np.abs(e)))
    rmse = float(np.sqrt(np.mean(e**2)))
    ss_res = float(np.sum(e**2))
    ss_tot = float(np.sum((y_true - np.mean(y_true)) ** 2))
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
    return {"mae": mae, "rmse": rmse, "r2": r2}


def cmd_validate(args: argparse.Namespace) -> None:
    import joblib
    from sklearn.ensemble import RandomForestRegressor

    pack = joblib.load(Path(args.model_dir) / "surrogate.joblib")
    rf_y: RandomForestRegressor = pack["rf_yes"]
    rf_n: RandomForestRegressor = pack["rf_no"]

    X, yy, yn, ts_list = _load_xy(args.features, int(args.max_lines or 0))
    ts_arr = np.array(ts_list)
    order = np.argsort(ts_arr)
    X, yy, yn = X[order], yy[order], yn[order]
    n = X.shape[0]
    cut = int(n * float(args.train_frac))
    Xte = X[cut:]
    yy_te, yn_te = yy[cut:], yn[cut:]

    py = rf_y.predict(Xte)
    pn = rf_n.predict(Xte)
    rep = {
        "yes_ask": _metrics(yy_te, py),
        "no_ask": _metrics(yn_te, pn),
        "n_test": int(Xte.shape[0]),
        "gates": {
            "mae_ok_yes": float(np.mean(np.abs(yy_te - py))) < float(args.mae_gate),
            "mae_ok_no": float(np.mean(np.abs(yn_te - pn))) < float(args.mae_gate),
        },
    }
    Path(args.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(json.dumps(rep, indent=2))


def _rf_quantiles(rf: Any, X: np.ndarray, qs: tuple[float, ...]) -> list[np.ndarray]:
    """Predicciones por árbol → percentiles por fila."""
    preds = np.array([est.predict(X) for est in rf.estimators_], dtype=float)
    return [np.percentile(preds, q, axis=0) for q in qs]


def _features_row(
    log_m: float, tl: float, vol_15m: float, is_btc: float, ts_sec: float
) -> np.ndarray:
    tl_norm = max(0.0, min(2.0, tl / 300.0))
    hs, hc, ds, dc = _time_features(ts_sec)
    return np.array(
        [[log_m, tl_norm, tl_norm**2, vol_15m, is_btc, hs, hc, ds, dc]],
        dtype=np.float64,
    )


def _interpolate_knots(times_sec: np.ndarray, values: np.ndarray, grid: np.ndarray) -> np.ndarray:
    return np.interp(grid, times_sec, values)


def _synthetic_option2_pnl(
    window_start_unix: float,
    times_dense: np.ndarray,
    yes_s: np.ndarray,
    no_s: np.ndarray,
    p: SimParams,
    ticker: str,
) -> dict[str, Any]:
    """Construye mercado con bids = ask - spread_synth y corre simulate_option2."""
    spread_synth = float(os.environ.get("BRIDGE_BID_SPREAD", "0.03"))
    yes_bid = np.maximum(0.01, yes_s - spread_synth)
    no_bid = np.maximum(0.01, no_s - spread_synth)
    base = float(window_start_unix)
    m = {
        "market_id": "synthetic",
        "ticker": ticker,
        "token_yes": "",
        "token_no": "",
        "yes_ask": [(base + float(t), float(y)) for t, y in zip(times_dense, yes_s)],
        "no_ask": [(base + float(t), float(n)) for t, n in zip(times_dense, no_s)],
        "yes_bid": [(base + float(t), float(y)) for t, y in zip(times_dense, yes_bid)],
        "no_bid": [(base + float(t), float(n)) for t, n in zip(times_dense, no_bid)],
    }
    st = simulate_option2([m], p, {})
    return st


def cmd_simulate_binance(args: argparse.Namespace) -> None:
    import joblib

    pack = joblib.load(Path(args.model_dir) / "surrogate.joblib")
    rf_y = pack["rf_yes"]
    rf_n = pack["rf_no"]

    days = int(args.days)
    end_ms = int(time.time() * 1000)
    start_ms = end_ms - days * 86400 * 1000
    symbol = str(args.symbol).upper()
    if symbol not in SYMBOLS:
        raise SystemExit("symbol debe ser BTC o ETH")
    bsym = SYMBOLS[symbol]
    print(f"Downloading {bsym} 1m ({days}d) …")
    kl = _download_klines_1m(bsym, start_ms, end_ms)
    oms, opens, closes = _build_close_lookup(kl)
    if oms.size < 500:
        raise SystemExit("Muy pocas velas descargadas.")

    p = SimParams(
        timeout_sec=float(args.timeout_sec),
        usd_per_leg=float(args.usd_per_leg),
        limit_price=float(args.limit_price),
        other_within=float(args.other_within),
        confirm_sec=float(args.confirm_sec),
        max_shares_per_leg=float(args.max_shares),
        fee_enabled=False,
    )

    step = int(args.window_step_sec)
    win = 300
    knots = np.array([0, 60, 120, 180, 240, 300], dtype=float)
    dense = np.linspace(0, 300, int(args.dense_points), dtype=float)

    pnls_med: list[float] = []
    pnls_lo: list[float] = []
    pnls_hi: list[float] = []

    t_start_sec = float(oms[0] / 1000.0)
    t_end_sec = float(oms[-1] / 1000.0)
    is_btc = 1.0 if symbol == "BTC" else 0.0

    def _minute_open_ms(ts_sec: float) -> int:
        return int(int(ts_sec) // 60 * 60 * 1000)

    # ventanas [cur, cur+win] alineadas al inicio de minuto (strike = open de esa vela)
    cur = math.ceil(t_start_sec / 60.0) * 60.0
    n_win = 0
    while cur + win <= t_end_sec:
        ms0 = _minute_open_ms(cur)
        i0 = bisect.bisect_left(oms, ms0)
        if i0 >= oms.size or int(oms[i0]) != ms0:
            cur += step
            continue
        strike = float(opens[i0])
        if strike <= 0:
            cur += step
            continue

        yes_knot: list[float] = []
        no_knot: list[float] = []
        ok = True
        for off in knots:
            ts_sec = cur + off
            msm = _minute_open_ms(ts_sec)
            i = bisect.bisect_left(oms, msm)
            if i < 0 or i >= oms.size or int(oms[i]) != msm:
                ok = False
                break
            spot = float(closes[i])
            log_m = math.log(spot / strike)
            vol = _vol_15m(oms, closes, ts_sec)
            tl = max(0.0, (cur + win) - ts_sec)
            Xr = _features_row(log_m, tl, vol, is_btc, ts_sec)
            qy = _rf_quantiles(rf_y, Xr, (10, 50, 90))
            qn = _rf_quantiles(rf_n, Xr, (10, 50, 90))
            yes_knot.append(float(qy[1][0]))
            no_knot.append(float(qn[1][0]))
        if not ok:
            cur += step
            continue

        y_med = _interpolate_knots(knots, np.array(yes_knot), dense)
        n_med = _interpolate_knots(knots, np.array(no_knot), dense)
        st0 = _synthetic_option2_pnl(cur, dense, y_med, n_med, p, symbol)
        pnls_med.append(float(st0.get("pnl_total", 0.0)))

        y_lo = np.clip(y_med - 0.08, 0.01, 0.99)
        n_hi = np.clip(n_med + 0.08, 0.01, 0.99)
        st1 = _synthetic_option2_pnl(cur, dense, y_lo, n_hi, p, symbol)
        pnls_lo.append(float(st1.get("pnl_total", 0.0)))

        y_hi = np.clip(y_med + 0.08, 0.01, 0.99)
        n_lo = np.clip(n_med - 0.08, 0.01, 0.99)
        st2 = _synthetic_option2_pnl(cur, dense, y_hi, n_lo, p, symbol)
        pnls_hi.append(float(st2.get("pnl_total", 0.0)))

        n_win += 1
        cur += step
        if n_win % 500 == 0:
            print(f"  … windows {n_win}")

    def agg(name: str, arr: list[float]) -> dict[str, Any]:
        if not arr:
            return {"scenario": name, "n_windows": 0}
        a = np.array(arr, dtype=float)
        nz = int(np.sum(np.abs(a) > 1e-12))
        return {
            "scenario": name,
            "n_windows": int(a.size),
            "n_windows_nonzero_pnl": nz,
            "pnl_sum": float(np.sum(a)),
            "pnl_mean": float(np.mean(a)),
            "pnl_std": float(np.std(a)),
            "p10": float(np.percentile(a, 10)),
            "p50": float(np.percentile(a, 50)),
            "p90": float(np.percentile(a, 90)),
        }

    rep = {
        "symbol": symbol,
        "days": days,
        "windows_evaluated": n_win,
        "straddle_params": {
            "timeout_sec": p.timeout_sec,
            "usd_per_leg": p.usd_per_leg,
            "limit_price": p.limit_price,
            "other_within": p.other_within,
            "confirm_sec": p.confirm_sec,
        },
        "scenarios": {
            "median_surrogate": agg("median", pnls_med),
            "pessimistic_asks_shift": agg("pessimistic", pnls_lo),
            "optimistic_asks_shift": agg("optimistic", pnls_hi),
        },
        "disclaimer": "Simulación sintética: surrogate entrenado en snapshots; no es PnL esperado en Polymarket real.",
    }
    Path(args.out).write_text(json.dumps(rep, indent=2), encoding="utf-8")
    print(json.dumps(rep, indent=2))


def cmd_describe_fix(args: argparse.Namespace) -> None:
    """Versión sin re-lectura masiva para histograma (corrige describe)."""
    limit_price = float(args.limit_price)
    touch_yes_logm: list[float] = []
    touch_yes_tl: list[float] = []
    touch_yes_other: list[float] = []
    touch_no_logm: list[float] = []
    touch_no_tl: list[float] = []
    touch_no_other: list[float] = []
    spread: list[float] = []
    all_logm: list[float] = []

    max_lines = int(args.max_lines or 0)
    n = 0
    for row in _iter_features(args.features, max_lines):
        n += 1
        ya, na = row["yes_ask"], row["no_ask"]
        spread.append(ya + na)
        all_logm.append(row["log_m"])
        if ya <= limit_price:
            touch_yes_logm.append(row["log_m"])
            touch_yes_tl.append(row["time_left_s"])
            touch_yes_other.append(na)
        if na <= limit_price:
            touch_no_logm.append(row["log_m"])
            touch_no_tl.append(row["time_left_s"])
            touch_no_other.append(ya)

    def summ(xs: list[float]) -> dict[str, float]:
        if not xs:
            return {"count": 0}
        a = np.array(xs, dtype=float)
        return {
            "count": int(a.size),
            "mean": float(np.mean(a)),
            "std": float(np.std(a)),
            "p10": float(np.percentile(a, 10)),
            "p50": float(np.percentile(a, 50)),
            "p90": float(np.percentile(a, 90)),
        }

    lm = np.array(all_logm, dtype=float) if all_logm else np.array([])
    hist = np.histogram(lm, bins=30, range=(-0.02, 0.02)) if lm.size else ([], [])

    out = {
        "rows": n,
        "limit_price": limit_price,
        "log_m_histogram_30bins_-0.02_0.02": [hist[0].tolist(), hist[1].tolist()],
        "yes_ask_touch": {"log_m": summ(touch_yes_logm), "tl_s": summ(touch_yes_tl), "other_ask": summ(touch_yes_other)},
        "no_ask_touch": {"log_m": summ(touch_no_logm), "tl_s": summ(touch_no_tl), "other_ask": summ(touch_no_other)},
        "spread_yes_plus_no": summ(spread),
        "note": "Touches por fila instantánea (no primer evento por mercado).",
    }
    Path(args.out).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(json.dumps(out, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Puente straddle snapshots ↔ Binance")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_join = sub.add_parser("join", help="Fase 1: unir snapshots con strike/spot/vol → features.jsonl.gz")
    p_join.add_argument("--snapshots", required=True, help="Ruta a orderbook_snapshots.jsonl")
    p_join.add_argument("--out-dir", required=True)
    p_join.add_argument("--stride", type=int, default=1, help="1 = todas las filas elegibles")
    p_join.add_argument("--max-rows", type=int, default=0, help="0 = sin límite en lectura inicial")
    p_join.set_defaults(func=cmd_join)

    p_d = sub.add_parser("describe", help="Fase 2: estadísticos descriptivos")
    p_d.add_argument("--features", required=True)
    p_d.add_argument("--out", required=True)
    p_d.add_argument("--limit-price", type=float, default=0.35)
    p_d.add_argument("--max-lines", type=int, default=0)
    p_d.set_defaults(func=cmd_describe_fix)

    p_t = sub.add_parser("train", help="Fase 3: entrenar RandomForest yes/no")
    p_t.add_argument("--features", required=True)
    p_t.add_argument("--out-dir", required=True)
    p_t.add_argument("--train-frac", type=float, default=0.7)
    p_t.add_argument("--max-lines", type=int, default=0)
    p_t.add_argument("--n-trees", type=int, default=80)
    p_t.add_argument("--max-depth", type=int, default=12)
    p_t.add_argument("--min-samples-leaf", type=int, default=50)
    p_t.set_defaults(func=cmd_train)

    p_v = sub.add_parser("validate", help="Fase 5: métricas holdout temporal")
    p_v.add_argument("--features", required=True)
    p_v.add_argument("--model-dir", required=True)
    p_v.add_argument("--out", required=True)
    p_v.add_argument("--train-frac", type=float, default=0.7)
    p_v.add_argument("--max-lines", type=int, default=0)
    p_v.add_argument("--mae-gate", type=float, default=0.12)
    p_v.set_defaults(func=cmd_validate)

    p_s = sub.add_parser("simulate-binance", help="Fase 4+5: simular ventanas 5m sobre N días Binance")
    p_s.add_argument("--model-dir", required=True)
    p_s.add_argument("--days", type=int, default=90)
    p_s.add_argument("--symbol", default="BTC", choices=["BTC", "ETH"])
    p_s.add_argument("--out", required=True)
    p_s.add_argument("--window-step-sec", type=int, default=60)
    p_s.add_argument("--dense-points", type=int, default=61)
    p_s.add_argument("--timeout-sec", type=float, default=120.0)
    p_s.add_argument("--usd-per-leg", type=float, default=2.0)
    p_s.add_argument("--limit-price", type=float, default=0.35)
    p_s.add_argument("--other-within", type=float, default=0.02)
    p_s.add_argument("--confirm-sec", type=float, default=60.0)
    p_s.add_argument("--max-shares", type=float, default=6.0)
    p_s.set_defaults(func=cmd_simulate_binance)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
