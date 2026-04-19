#!/usr/bin/env python3
"""
Análisis sobre CSV exportados por binance_5m_candle_stats.py:

1) Rachas consecutivas de velas con cierre > apertura (verdes) o < apertura (rojas).
2) Volatilidad intravela (high-low)/open % por hora UTC de apertura de la vela.

Ejemplo:
  cd polymarket-trading
  python3 scripts/analyze_binance_5m_csv.py \\
    --inputs data/binance/btcusdt_5m_90d.csv data/binance/ethusdt_5m_90d.csv \\
    --out-md ../docs/BINANCE_5M_90D_RACHAS_Y_VOL_HORA.md
"""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


def load_ohlc(path: Path) -> tuple[str, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Devuelve symbol, open_ms, O, H, L, C como float arrays alineados por tiempo."""
    opens_ms: list[int] = []
    o: list[float] = []
    h: list[float] = []
    low: list[float] = []
    c: list[float] = []
    sym = ""
    with path.open(newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        for row in r:
            if not sym:
                sym = row.get("symbol", path.stem.upper())
            opens_ms.append(int(row["open_time_ms"]))
            o.append(float(row["open"]))
            h.append(float(row["high"]))
            low.append(float(row["low"]))
            c.append(float(row["close"]))
    order = np.argsort(np.array(opens_ms, dtype=np.int64))
    O = np.array(o, dtype=float)[order]
    H = np.array(h, dtype=float)[order]
    L = np.array(low, dtype=float)[order]
    C = np.array(c, dtype=float)[order]
    T = np.array(opens_ms, dtype=np.int64)[order]
    return sym, T, O, H, L, C


def direction_sign(C: np.ndarray, O: np.ndarray) -> np.ndarray:
    """+1 verde, -1 roja, 0 doji (casi igual con tolerancia float)."""
    eps = 1e-9
    d = C - O
    out = np.zeros(len(C), dtype=np.int8)
    out[d > eps] = 1
    out[d < -eps] = -1
    return out


def streak_lengths(signs: np.ndarray) -> tuple[list[int], list[int]]:
    """Listas de longitudes de rachas consecutivas de +1 y de -1 (alternando, sin mezclar en una misma racha)."""
    up_lens: list[int] = []
    down_lens: list[int] = []
    cur_sign = 0
    cur_len = 0
    for s in signs:
        if s == 0:
            if cur_sign == 1 and cur_len > 0:
                up_lens.append(cur_len)
            elif cur_sign == -1 and cur_len > 0:
                down_lens.append(cur_len)
            cur_sign = 0
            cur_len = 0
            continue
        if s == cur_sign:
            cur_len += 1
        else:
            if cur_sign == 1 and cur_len > 0:
                up_lens.append(cur_len)
            elif cur_sign == -1 and cur_len > 0:
                down_lens.append(cur_len)
            cur_sign = int(s)
            cur_len = 1
    if cur_sign == 1 and cur_len > 0:
        up_lens.append(cur_len)
    elif cur_sign == -1 and cur_len > 0:
        down_lens.append(cur_len)
    return up_lens, down_lens


def streak_summary(name: str, lengths: list[int]) -> dict[str, Any]:
    if not lengths:
        return {"name": name, "n_rachas": 0}
    a = np.array(lengths, dtype=int)
    cnt = Counter(lengths)
    bucket = {str(k): cnt[k] for k in range(1, 6)}
    bucket["6+"] = sum(cnt[k] for k in cnt if k >= 6)
    return {
        "name": name,
        "n_rachas": int(a.size),
        "max": int(a.max()),
        "mean": float(a.mean()),
        "median": float(np.median(a)),
        "p90": float(np.percentile(a, 90)),
        "p95": float(np.percentile(a, 95)),
        "histogram_1_to_5_and_6plus": bucket,
    }


def hour_utc_from_open_ms(ms: int) -> int:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).hour


def volatility_by_hour(O: np.ndarray, H: np.ndarray, L: np.ndarray, T: np.ndarray) -> dict[int, dict[str, float]]:
    eps = 1e-12
    denom = np.where(np.abs(O) < eps, np.nan, O)
    rng_pct = (H - L) / denom * 100.0
    by_h: dict[int, list[float]] = {h: [] for h in range(24)}
    for i in range(len(O)):
        if not np.isfinite(rng_pct[i]):
            continue
        hr = hour_utc_from_open_ms(int(T[i]))
        by_h[hr].append(float(rng_pct[i]))
    out: dict[int, dict[str, float]] = {}
    for hr, vals in by_h.items():
        if not vals:
            out[hr] = {"mean": float("nan"), "median": float("nan"), "n": 0}
            continue
        v = np.array(vals, dtype=float)
        out[hr] = {
            "n": int(v.size),
            "mean": float(np.mean(v)),
            "median": float(np.median(v)),
            "p90": float(np.percentile(v, 90)),
        }
    return out


def md_table_vol_ranking(vol_by_h: dict[int, dict[str, float]], top_n: int = 5) -> str:
    rows = [(h, d["mean"], d["median"], d["n"]) for h, d in vol_by_h.items() if d["n"] > 0 and np.isfinite(d["mean"])]
    rows.sort(key=lambda x: x[1], reverse=True)
    most = rows[:top_n]
    least = list(reversed(rows[-top_n:])) if len(rows) >= top_n else list(reversed(rows))
    lines = [
        "| Ranking | Hora UTC | Media (H−L)/open % | Mediana % | N velas |",
        "|---------|----------|---------------------|-----------|---------|",
    ]
    for i, (h, m, med, n) in enumerate(most, 1):
        lines.append(f"| Más volátil #{i} | {h:02d}:00–{h:02d}:55 | {m:.4f} | {med:.4f} | {n} |")
    lines.append("")
    lines.append("| Ranking | Hora UTC | Media (H−L)/open % | Mediana % | N velas |")
    lines.append("|---------|----------|---------------------|-----------|---------|")
    for i, (h, m, med, n) in enumerate(least, 1):
        lines.append(f"| Menos volátil #{i} | {h:02d}:00–{h:02d}:55 | {m:.4f} | {med:.4f} | {n} |")
    return "\n".join(lines)


def _fmt4(x: float) -> str:
    return f"{x:.4f}" if np.isfinite(x) else "—"


def md_full_table_all_hours(vol_by_h: dict[int, dict[str, float]]) -> str:
    lines = [
        "| Hora UTC | Media (H−L)/open % | Mediana % | p90 % | N velas |",
        "|----------|-------------------|------------|-------|---------|",
    ]
    for h in range(24):
        d = vol_by_h.get(h, {})
        lines.append(
            f"| {h:02d} | {_fmt4(float(d.get('mean', float('nan'))))} | "
            f"{_fmt4(float(d.get('median', float('nan'))))} | "
            f"{_fmt4(float(d.get('p90', float('nan'))))} | {int(d.get('n', 0))} |"
        )
    return "\n".join(lines)


def build_markdown(
    results: list[dict[str, Any]],
    inputs: list[str],
) -> str:
    lines = [
        "# Análisis extendido — velas Binance 5m (90 días)",
        "",
        "Este documento complementa `BINANCE_5M_ULTIMOS_90D.md` (datos crudos y resumen OHLC).",
        "",
        "## Origen de los datos",
        "",
    ]
    for p in inputs:
        lines.append(f"- `{p}`")
    lines.extend(
        [
            "",
            "## 1. Rachas consecutivas (cierre vs apertura)",
            "",
            "Una **racha verde** = velas seguidas con **close > open**. Una **racha roja** = **close < open**.",
            "Los **dojis** (`close ≈ open`) cortan la racha sin añadir longitud a ninguna lista.",
            "",
        ]
    )
    for block in results:
        sym = block["symbol"]
        su, sd = block["streak_up_summary"], block["streak_down_summary"]
        lines.extend([f"### {sym}", ""])
        for s in (su, sd):
            lines.append(f"#### {s['name']}")
            if s.get("n_rachas", 0) == 0:
                lines.append("*Sin rachas.*")
                lines.append("")
                continue
            hist = s.get("histogram_1_to_5_and_6plus", {})
            hist_s = ", ".join(f"{k}: {v}" for k, v in sorted(hist.items(), key=lambda x: (len(x[0]), x[0])))
            lines.extend(
                [
                    f"- **Número de rachas:** {s['n_rachas']}",
                    f"- **Máxima longitud:** {s['max']} velas",
                    f"- **Media:** {s['mean']:.3f} | **Mediana:** {s['median']:.1f} | **p90:** {s['p90']:.1f} | **p95:** {s['p95']:.1f}",
                    f"- **Histograma** (cuántas rachas duraron 1,2,3,4,5,6+ velas): {hist_s}",
                    "",
                ]
            )
        lines.extend(
            [
                f"### {sym} — volatilidad por hora UTC",
                "",
                "Para cada vela se usa la **hora UTC del `open_time`** y el rango **(high − low) / open × 100**.",
                "",
                "#### Horas más y menos volátiles (por media)",
                "",
                block["vol_ranking_md"],
                "",
                "#### Tabla completa 0–23h UTC",
                "",
                block["vol_table_md"],
                "",
            ]
        )
    lines.append("---")
    lines.append("")
    lines.append("*Generado con `scripts/analyze_binance_5m_csv.py`.*")
    lines.append("")
    return "\n".join(lines)


def analyze_one_csv(path: Path) -> dict[str, Any]:
    sym, T, O, H, L, C = load_ohlc(path)
    signs = direction_sign(C, O)
    up_lens, down_lens = streak_lengths(signs)
    su = streak_summary("Rachas verdes (close > open)", up_lens)
    sd = streak_summary("Rachas rojas (close < open)", down_lens)
    vol_h = volatility_by_hour(O, H, L, T)
    return {
        "symbol": sym,
        "path": str(path),
        "n_velas": int(len(C)),
        "streak_up_summary": su,
        "streak_down_summary": sd,
        "vol_ranking_md": md_table_vol_ranking(vol_h),
        "vol_table_md": md_full_table_all_hours(vol_h),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Análisis rachas + volatilidad por hora en CSV 5m")
    ap.add_argument("--inputs", nargs="+", required=True, help="Uno o más CSV (cabecera estándar del export)")
    ap.add_argument("--out-md", required=True, help="Ruta al Markdown de salida")
    args = ap.parse_args()

    paths = [Path(p) for p in args.inputs]
    for p in paths:
        if not p.is_file():
            raise SystemExit(f"No existe: {p}")

    results = [analyze_one_csv(p) for p in paths]
    out_path = Path(args.out_md)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rel_inputs = [str(p) for p in paths]
    out_path.write_text(build_markdown(results, rel_inputs), encoding="utf-8")
    print(f"Escrito: {out_path}")


if __name__ == "__main__":
    main()
