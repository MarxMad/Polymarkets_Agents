#!/usr/bin/env python3
"""
Compara en la misma data (orderbook_snapshots.jsonl):

1) Estrategia straddle opción 2 con parámetros fijos (los del bot / servicio).
2) Reglas del sniper (ventana tiempo, edge+buffer, edad mínima mercado, tope ask,
   GBM + prob estable) evaluadas fila a fila con Binance **histórico** al timestamp
   de cada snapshot y resolución al endDate del mercado.

No usa el dashboard. Pensado para correr en la instancia donde está el JSONL.

Ejemplo:
  python3 compare_straddle_sniper_on_snapshots.py \\
    --in-file ~/orderbook_snapshots.jsonl --stride 5 --sniper-mc-sims 1200
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np
import requests

# Reutiliza carga y simulación straddle sobre mercados agregados
from straddle_optimizer import SimParams, load_markets, parse_jsonl, simulate_option2, ts_to_num

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_TICKERS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}

# --- Parámetros alineados al servicio straddle actual (pumaclaw-straddle.service) ---
STRADDLE_PARAMS = SimParams(
    timeout_sec=45.0,
    usd_per_leg=2.0,
    limit_price=0.35,
    other_within=0.02,
    confirm_sec=60.0,
    max_shares_per_leg=6.0,
)

# --- Parámetros alineados a montecarlo_sniper.py ---
SNIPER_MIN_EDGE = 0.10
SNIPER_EDGE_BUFFER = 0.03
SNIPER_MIN_MARKET_AGE_SEC = 120.0
SNIPER_TL_MIN = 180.0
SNIPER_TL_MAX = 1200.0
SNIPER_ASK_CAP = 0.85
SNIPER_TRADE_USD = 2.0
SNIPER_MAX_SHARES = 4.0
SNIPER_FEE_BPS = 25.0  # aproximación taker; no llama CLOB por fila


def _gamma_event_start(market_id: str):
    if not market_id:
        return None
    try:
        r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=10)
        if r.status_code != 200:
            return None
        m = r.json()
        if not isinstance(m, dict):
            return None
        return m.get("eventStartTime") or m.get("startDate")
    except Exception:
        return None


def _binance_kline_open(symbol: str, iso_utc: str):
    try:
        ts = int(datetime.fromisoformat(iso_utc.replace("Z", "+00:00")).timestamp() * 1000)
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": ts, "limit": 1},
            timeout=5,
        )
        if not r.ok or not r.json():
            return None
        return float(r.json()[0][1])
    except Exception:
        return None


def _binance_close_at_ms(symbol: str, end_ms: int):
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "startTime": int(end_ms), "limit": 1},
            timeout=5,
        )
        if not r.ok or not r.json():
            return None
        return float(r.json()[0][4])
    except Exception:
        return None


def _historic_spot_vol_drift(symbol: str, ts_unix: float):
    """Spot (close vela previa) + vol/drift anualizados como get_binance_data del sniper, en tiempo ts."""
    ts_ms = int(ts_unix * 1000)
    try:
        r = requests.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "1m", "endTime": ts_ms, "limit": 62},
            timeout=6,
        )
        if not r.ok:
            return None, None, None
        kl = r.json()
        if len(kl) < 12:
            return None, None, None
        closes = np.array([float(k[4]) for k in kl[:-1]], dtype=float)[-60:]
        if len(closes) < 10:
            return None, None, None
        spot = float(kl[-2][4])
        rets = np.diff(np.log(closes))
        vol = float(np.std(rets) * np.sqrt(525600))
        vol = max(0.10, min(vol, 2.50))
        drift = float(np.mean(rets) * 525600)
        drift = max(-1.0, min(drift, 1.0))
        return spot, vol, drift
    except Exception:
        return None, None, None


def _monte_carlo_prob_yes(s0, strike, tsec, vol, drift, simulations: int) -> float:
    if tsec <= 0:
        return 1.0 if s0 > strike else 0.0
    T = tsec / (365 * 24 * 60 * 60)
    steps = max(10, int(tsec / 5))
    steps = min(steps, 240)
    dt = T / steps
    rng = np.random.default_rng()
    shocks = rng.normal(0, 1, (simulations, steps))
    paths = np.zeros((simulations, steps + 1), dtype=float)
    paths[:, 0] = s0
    for t in range(1, steps + 1):
        paths[:, t] = paths[:, t - 1] * np.exp(
            (drift - 0.5 * vol**2) * dt + vol * np.sqrt(dt) * shocks[:, t - 1]
        )
    finals = paths[:, -1]
    return float(np.mean(finals > strike))


def _stable_prob_yes(s0, strike, tsec, vol, drift, simulations: int) -> float:
    a = _monte_carlo_prob_yes(s0, strike, tsec, vol, drift, simulations)
    b = _monte_carlo_prob_yes(s0, strike, tsec, vol, 0.0, simulations)
    return 0.30 * a + 0.70 * b


def _fee_usd(notional: float) -> float:
    return notional * (SNIPER_FEE_BPS / 10000.0)


def run_straddle(in_file: str) -> dict:
    markets, n_lines = load_markets(in_file)
    fee_cache: dict[str, int] = {}
    st = simulate_option2(markets, STRADDLE_PARAMS, fee_cache=fee_cache)
    return {
        "lines": n_lines,
        "markets": len(markets),
        "params": {
            "timeout_sec": STRADDLE_PARAMS.timeout_sec,
            "usd_per_leg": STRADDLE_PARAMS.usd_per_leg,
            "limit_price": STRADDLE_PARAMS.limit_price,
            "other_within": STRADDLE_PARAMS.other_within,
            "confirm_sec": STRADDLE_PARAMS.confirm_sec,
            "max_shares_per_leg": STRADDLE_PARAMS.max_shares_per_leg,
        },
        "triggers": st["triggers"],
        "trades": st["trades"],
        "conv_2legs": st["conv_2legs"],
        "stops": st["stops"],
        "pnl_total": round(st["pnl_total"], 4),
        "pnl_btc": round(st["by_ticker"]["BTC"]["pnl"], 4),
        "pnl_eth": round(st["by_ticker"]["ETH"]["pnl"], 4),
        "max_drawdown": round(st["max_drawdown"], 4),
    }


def run_sniper_on_jsonl(
    in_file: str,
    stride: int,
    max_rows: int,
    sniper_mc_sims: int,
    btc_only: bool,
) -> dict:
    stride = max(1, stride)
    event_cache: dict[str, object] = {}
    strike_cache: dict[tuple[str, str], float] = {}

    def event_start(mid: str):
        if mid not in event_cache:
            event_cache[mid] = _gamma_event_start(mid)
        return event_cache[mid]

    def strike_for(mid: str, ticker: str) -> float:
        key = (str(mid), ticker)
        if key in strike_cache:
            return strike_cache[key]
        es = event_start(mid)
        sym = BINANCE_TICKERS.get(ticker)
        st = 0.0
        if es and sym:
            o = _binance_kline_open(sym, es)
            st = float(o) if o is not None else 0.0
        strike_cache[key] = st
        return st

    rows_seen = 0
    rows_used = 0
    skipped = {k: 0 for k in ("ticker", "time", "age", "binance", "strike", "asks", "skip_decision")}
    entries_yes = entries_no = 0
    pnl_list: list[float] = []
    wins = losses = 0

    for o in parse_jsonl(in_file):
        rows_seen += 1
        if max_rows > 0 and rows_seen > max_rows:
            break
        if (rows_seen - 1) % stride != 0:
            continue

        ticker = (o.get("ticker") or "").upper()
        if btc_only and ticker != "BTC":
            skipped["ticker"] += 1
            continue
        if ticker not in BINANCE_TICKERS:
            skipped["ticker"] += 1
            continue

        ts = ts_to_num(o.get("ts"))
        if ts is None:
            continue
        tl = o.get("time_left_s")
        try:
            tl = float(tl)
        except (TypeError, ValueError):
            skipped["time"] += 1
            continue
        if not (SNIPER_TL_MIN <= tl <= SNIPER_TL_MAX):
            skipped["time"] += 1
            continue

        mid = str(o.get("market_id") or "")
        es = event_start(mid)
        if es:
            try:
                start_dt = datetime.fromisoformat(es.replace("Z", "+00:00"))
                age = float(ts) - start_dt.timestamp()
                if age < SNIPER_MIN_MARKET_AGE_SEC:
                    skipped["age"] += 1
                    continue
            except Exception:
                pass

        sym = BINANCE_TICKERS[ticker]
        spot, vol, drift = _historic_spot_vol_drift(sym, float(ts))
        if spot is None or vol is None:
            skipped["binance"] += 1
            continue

        strike = strike_for(mid, ticker)
        if strike <= 0:
            skipped["strike"] += 1
            continue

        ya = o.get("yes_ask")
        na = o.get("no_ask")
        try:
            ask_yes = float(ya)
            ask_no = float(na)
        except (TypeError, ValueError):
            skipped["asks"] += 1
            continue

        p_yes = _stable_prob_yes(spot, strike, tl, vol, drift, sniper_mc_sims)
        edge_yes = p_yes - ask_yes - SNIPER_EDGE_BUFFER
        edge_no = (1.0 - p_yes) - ask_no - SNIPER_EDGE_BUFFER

        side = None
        entry = 0.0
        if edge_yes > SNIPER_MIN_EDGE and ask_yes < SNIPER_ASK_CAP:
            side, entry = "YES", ask_yes
            entries_yes += 1
        elif edge_no > SNIPER_MIN_EDGE and ask_no < SNIPER_ASK_CAP:
            side, entry = "NO", ask_no
            entries_no += 1
        else:
            skipped["skip_decision"] += 1
            continue

        shares = min(SNIPER_TRADE_USD / entry, SNIPER_MAX_SHARES)
        shares = max(1.0, round(shares, 2))
        notional = shares * entry
        fee = _fee_usd(notional)

        end_s = o.get("endDate")
        if not end_s:
            skipped["asks"] += 1
            continue
        try:
            end_dt = datetime.fromisoformat(end_s.replace("Z", "+00:00"))
            end_ms = int(end_dt.timestamp() * 1000)
        except Exception:
            skipped["asks"] += 1
            continue

        px_end = _binance_close_at_ms(sym, end_ms)
        if px_end is None:
            skipped["binance"] += 1
            continue

        if side == "YES":
            won = px_end > strike
            gross = shares * 1.0 if won else 0.0
        else:
            won = px_end < strike
            gross = shares * 1.0 if won else 0.0

        pnl = gross - notional - fee
        pnl_list.append(pnl)
        if won:
            wins += 1
        else:
            losses += 1
        rows_used += 1

    total_pnl = float(sum(pnl_list))
    return {
        "stride": stride,
        "max_rows": max_rows,
        "rows_seen": rows_seen,
        "rows_seen_in_file": rows_seen,
        "sniper_decisions_simulated": rows_used,
        "entries_yes": entries_yes,
        "entries_no": entries_no,
        "skipped": skipped,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / rows_used) if rows_used else 0.0,
        "pnl_total": round(total_pnl, 4),
        "pnl_mean_per_entry": round(total_pnl / rows_used, 6) if rows_used else 0.0,
        "params": {
            "min_edge": SNIPER_MIN_EDGE,
            "edge_buffer": SNIPER_EDGE_BUFFER,
            "min_market_age_sec": SNIPER_MIN_MARKET_AGE_SEC,
            "time_left_min_max": [SNIPER_TL_MIN, SNIPER_TL_MAX],
            "ask_cap": SNIPER_ASK_CAP,
            "trade_usd": SNIPER_TRADE_USD,
            "max_shares": SNIPER_MAX_SHARES,
            "fee_bps": SNIPER_FEE_BPS,
            "mc_sims_per_row": sniper_mc_sims,
            "btc_only": btc_only,
        },
        "note": "Cada fila es independiente (mismo mercado puede contar varias veces). Usa --stride alto para ir más rápido y suavizar correlación.",
    }


def main():
    ap = argparse.ArgumentParser(description="Straddle O2 + sniper sobre el mismo orderbook_snapshots.jsonl")
    ap.add_argument("--in-file", default=os.path.expanduser(os.getenv("OB_IN_FILE", "~/orderbook_snapshots.jsonl")))
    ap.add_argument("--stride", type=int, default=10, help="Procesa 1 de cada N filas en el sniper (Binance/Gamma).")
    ap.add_argument("--max-rows", type=int, default=0, help="0 = sin tope adicional tras stride (recorre todo el archivo).")
    ap.add_argument("--sniper-mc-sims", type=int, default=1200, help="Simulaciones GBM por fila en sniper (CPU).")
    ap.add_argument("--btc-only", action="store_true", help="Sniper solo BTC (straddle sigue ambos).")
    ap.add_argument("--out-json", default="", help="Opcional: guardar resultado combinado en JSON.")
    args = ap.parse_args()

    path = os.path.expanduser(args.in_file)
    if not os.path.isfile(path):
        print(f"No existe el archivo: {path}", file=sys.stderr)
        print("Copia el JSONL desde la instancia o define OB_IN_FILE.", file=sys.stderr)
        sys.exit(1)

    print("=== 1) STRADDLE (opción 2, parámetros actuales del bot) ===", flush=True)
    st_out = run_straddle(path)
    print(json.dumps(st_out, indent=2), flush=True)

    print("\n=== 2) SNIPER (reglas actuales + Binance/Gamma histórico por fila) ===", flush=True)
    sn_out = run_sniper_on_jsonl(
        path,
        stride=args.stride,
        max_rows=args.max_rows,
        sniper_mc_sims=max(200, min(args.sniper_mc_sims, 5000)),
        btc_only=args.btc_only,
    )
    print(json.dumps(sn_out, indent=2), flush=True)

    combined = {"in_file": path, "straddle": st_out, "sniper": sn_out}
    if args.out_json:
        with open(args.out_json, "w") as f:
            json.dump(combined, f, indent=2)
        print(f"\nGuardado: {args.out_json}", flush=True)


if __name__ == "__main__":
    main()
