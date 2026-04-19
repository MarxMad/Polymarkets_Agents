#!/usr/bin/env python3
"""
Estadísticas sobre velas Binance 5m (OHLC): cierres arriba/abajo de la apertura
y variación % intravela (máximo − mínimo) respecto al open.

No usa Polymarket ni CLOB; sirve para calibrar reglas direccionales / volatilidad de 5m.

Ejemplos:
  python3 binance_5m_candle_stats.py --symbol BTCUSDT --days 90
  python3 binance_5m_candle_stats.py --days 90 --symbols BTCUSDT,ETHUSDT \\
      --export-dir data/binance --stats-md ../docs/BINANCE_5M_ULTIMOS_90D.md

  # Rachas consecutivas + volatilidad por hora (lee los CSV ya exportados):
  python3 scripts/analyze_binance_5m_csv.py \\
      --inputs data/binance/btcusdt_5m_90d.csv data/binance/ethusdt_5m_90d.csv \\
      --out-md ../docs/BINANCE_5M_90D_RACHAS_Y_VOL_HORA.md
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import requests

BINANCE = "https://api.binance.com/api/v3/klines"

# Columnas tal como las devuelve GET /api/v3/klines (documentación Binance).
KLINES_CSV_HEADER = [
    "symbol",
    "open_time_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time_ms",
    "quote_asset_volume",
    "n_trades",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]


def fetch_klines_5m_full(symbol: str, start_ms: int, end_ms: int) -> list[list[Any]]:
    """Filas crudas de la API (cada fila: 12 campos Binance)."""
    out: list[list[Any]] = []
    cur = int(start_ms)
    end_ms = int(end_ms)
    while cur <= end_ms:
        r = requests.get(
            BINANCE,
            params={"symbol": symbol, "interval": "5m", "startTime": cur, "limit": 1000},
            timeout=20,
        )
        r.raise_for_status()
        kl = r.json()
        if not kl:
            break
        for row in kl:
            out.append(list(row))
        cur = int(kl[-1][0]) + 300_000
        time.sleep(0.04)
    by_t: dict[int, list[Any]] = {}
    for row in out:
        by_t[int(row[0])] = row
    return [by_t[k] for k in sorted(by_t)]


def fetch_klines_5m(symbol: str, start_ms: int, end_ms: int) -> list[tuple[int, float, float, float, float]]:
    """Lista (open_time_ms, open, high, low, close)."""
    raw = fetch_klines_5m_full(symbol, start_ms, end_ms)
    return [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in raw]


def write_klines_csv(path: Path, symbol: str, rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(KLINES_CSV_HEADER)
        for r in rows:
            w.writerow(
                [
                    symbol,
                    int(r[0]),
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5],
                    int(r[6]),
                    r[7],
                    int(r[8]),
                    r[9],
                    r[10],
                    r[11],
                ]
            )


def build_stats_markdown(reports: list[dict[str, Any]], data_files: list[str]) -> str:
    lines = [
        "# Velas Binance 5m — últimos 90 días (datos y resumen)",
        "",
        "Datos descargados desde la API pública `GET /api/v3/klines` (intervalo `5m`).",
        "",
        "## Archivos de datos (CSV)",
        "",
    ]
    for p in data_files:
        lines.append(f"- `{p}` (ruta relativa a la carpeta `polymarket-trading/`)")
    lines.extend(
        [
            "",
            "Cada fila replica los campos de `GET /api/v3/klines` más la columna `symbol`.",
            "",
            "| Columna CSV | Campo Binance |",
            "|---------------|----------------|",
            "| `symbol` | Par (añadido por el script) |",
            "| `open_time_ms` | Inicio de la vela (ms UTC) |",
            "| `open`, `high`, `low`, `close` | OHLC |",
            "| `volume` | Volumen en activo base |",
            "| `close_time_ms` | Fin de la vela (ms UTC) |",
            "| `quote_asset_volume` | Volumen en activo cotización |",
            "| `n_trades` | Número de trades |",
            "| `taker_buy_base_volume` | Volumen compra taker (base) |",
            "| `taker_buy_quote_volume` | Volumen compra taker (quote) |",
            "| `ignore` | Campo ignorado (API) |",
            "",
            "## Resumen estadístico",
            "",
        ]
    )
    for rep in reports:
        sym = rep["symbol"]
        s = rep["summary"]
        rh = s["range_high_low_pct_of_basis"]
        bd = s["body_abs_pct_of_open"]
        db = s["directional_bias_close_minus_open_pct_of_open"]
        lines.extend(
            [
                f"### {sym}",
                "",
                "| Campo | Valor |",
                "|--------|-------|",
                f"| Primer `open_time` (UTC) | {rep['first_open_time_utc']} |",
                f"| Último `open_time` (UTC) | {rep['last_open_time_utc']} |",
                f"| Velas (`n_candles`) | {s['n_candles']} |",
                f"| Cierre **>** apertura (n) | {s['n_close_above_open']} ({s['pct_close_above_open']:.2f} %) |",
                f"| Cierre **<** apertura (n) | {s['n_close_below_open']} ({s['pct_close_below_open']:.2f} %) |",
                f"| Cierre **=** apertura (n) | {s['n_close_equal_open']} ({s['pct_close_equal_open']:.4f} %) |",
                f"| Base rango (H−L) % | `{s['range_pct_basis']}` (respecto al open de la vela salvo `mid`) |",
                f"| Rango (H−L) % — media | {rh['mean']:.6f} |",
                f"| Rango (H−L) % — mediana | {rh['median']:.6f} |",
                f"| Rango (H−L) % — p10 / p90 | {rh['p10']:.6f} / {rh['p90']:.6f} |",
                f"| Rango (H−L) % — std | {rh['std']:.6f} |",
                f"| Cuerpo abs. (C−O) % del open — media | {bd['mean']:.6f} |",
                f"| Cuerpo abs. (C−O) % del open — mediana | {bd['median']:.6f} |",
                f"| Sesgo (C−O) % del open — media | {db['mean']:.6f} |",
                f"| Sesgo (C−O) % del open — mediana | {db['median']:.6f} |",
                "",
            ]
        )
    lines.extend(
        [
            "## Interpretación breve",
            "",
            "- Si **% cierre > apertura** y **% cierre < apertura** son similares (~50 %), en 5m no hay sesgo direccional fuerte en la muestra.",
            "- **Rango (H−L) % del open**: mide cuánto se mueve el precio *dentro* de la vela; comparar BTC vs ETH sirve para calibrar stops o umbrales de “movimiento típico” en 5m.",
            "- **Sesgo (C−O) % del open** cercano a 0: la suma de retornos de cuerpo en 5m es casi neutra en el periodo (no implica ausencia de tendencias en escalas mayores).",
            "",
            "## Notas",
            "",
            "- Los porcentajes de rango y cuerpo usan el **open** de cada vela como denominador (salvo que en el script se use `--range-pct-basis mid`).",
            "- Esto **no** sustituye datos de Polymarket (CLOB, shares); sirve como referencia de volatilidad y sesgo direccional en ventanas de 5 minutos.",
            "- Para regenerar CSV y este resumen:",
            "",
            "```bash",
            "cd polymarket-trading",
            "python3 scripts/binance_5m_candle_stats.py --days 90 --symbols BTCUSDT,ETHUSDT \\",
            "  --export-dir data/binance --stats-md ../docs/BINANCE_5M_ULTIMOS_90D.md",
            "```",
            "",
            "Rachas consecutivas y volatilidad por hora:",
            "",
            "```bash",
            "python3 scripts/analyze_binance_5m_csv.py \\",
            "  --inputs data/binance/btcusdt_5m_90d.csv data/binance/ethusdt_5m_90d.csv \\",
            "  --out-md ../docs/BINANCE_5M_90D_RACHAS_Y_VOL_HORA.md",
            "```",
            "",
        ]
    )
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines.append(f"*Generado: {gen}*")
    lines.append("")
    return "\n".join(lines)


def _hour_utc(open_ms: int) -> int:
    return datetime.fromtimestamp(open_ms / 1000.0, tz=timezone.utc).hour


def summarize(
    rows: list[tuple[int, float, float, float, float]],
    range_pct_basis: str,
) -> dict[str, Any]:
    if not rows:
        return {"error": "sin velas"}

    o = np.array([r[1] for r in rows], dtype=float)
    h = np.array([r[2] for r in rows], dtype=float)
    low = np.array([r[3] for r in rows], dtype=float)
    c = np.array([r[4] for r in rows], dtype=float)

    eps = 1e-12
    denom = np.where(np.abs(o) < eps, np.nan, o)
    range_hl = (h - low) / denom * 100.0
    body = np.abs(c - o) / denom * 100.0

    if range_pct_basis == "mid":
        mid = (h + low) / 2.0
        denom2 = np.where(np.abs(mid) < eps, np.nan, mid)
        range_hl = (h - low) / denom2 * 100.0

    up = c > o
    down = c < o
    flat = ~(up | down)

    valid_range = np.isfinite(range_hl)
    return {
        "n_candles": int(len(rows)),
        "n_close_above_open": int(np.sum(up)),
        "n_close_below_open": int(np.sum(down)),
        "n_close_equal_open": int(np.sum(flat)),
        "pct_close_above_open": float(np.mean(up) * 100.0),
        "pct_close_below_open": float(np.mean(down) * 100.0),
        "pct_close_equal_open": float(np.mean(flat) * 100.0),
        "range_pct_basis": range_pct_basis,
        "range_high_low_pct_of_basis": {
            "mean": float(np.nanmean(range_hl[valid_range])),
            "median": float(np.nanmedian(range_hl[valid_range])),
            "p10": float(np.nanpercentile(range_hl[valid_range], 10)),
            "p90": float(np.nanpercentile(range_hl[valid_range], 90)),
            "std": float(np.nanstd(range_hl[valid_range])),
        },
        "body_abs_pct_of_open": {
            "mean": float(np.nanmean(body[np.isfinite(body)])),
            "median": float(np.nanmedian(body[np.isfinite(body)])),
        },
        "directional_bias_close_minus_open_pct_of_open": {
            "mean": float(np.nanmean((c - o) / denom * 100.0)),
            "median": float(np.nanmedian((c - o) / denom * 100.0)),
        },
    }


def by_hour(
    rows: list[tuple[int, float, float, float, float]],
    range_pct_basis: str,
) -> dict[str, Any]:
    buckets: dict[int, list[tuple[int, float, float, float, float]]] = defaultdict(list)
    for r in rows:
        buckets[_hour_utc(r[0])].append(r)
    out: dict[str, Any] = {}
    for hr in range(24):
        if hr not in buckets:
            continue
        out[str(hr)] = summarize(buckets[hr], range_pct_basis)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Estadísticas OHLC en velas Binance 5m")
    ap.add_argument("--symbol", default="BTCUSDT", help="Ej. BTCUSDT, ETHUSDT (si --symbols vacío)")
    ap.add_argument(
        "--symbols",
        default="",
        help="Lista separada por comas, ej. BTCUSDT,ETHUSDT. Si se omite, se usa --symbol.",
    )
    ap.add_argument("--days", type=int, default=90, help="Ventana hacia atrás desde ahora (UTC)")
    ap.add_argument(
        "--range-pct-basis",
        choices=("open", "mid"),
        default="open",
        help="Denominador para (high-low)%%: open de la vela o mid (high+low)/2",
    )
    ap.add_argument("--by-hour", action="store_true", help="Desglose por hora UTC de apertura de la vela")
    ap.add_argument("--out-json", default="", help="Si se indica, escribe el JSON del último símbolo procesado")
    ap.add_argument(
        "--export-dir",
        default="",
        help="Directorio local: escribe un CSV por símbolo con todas las columnas de la API",
    )
    ap.add_argument(
        "--stats-md",
        default="",
        help="Ruta a un .md con tablas de resumen (típico junto a --export-dir y varios --symbols)",
    )
    args = ap.parse_args()

    syms = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    if not syms:
        syms = [args.symbol.upper()]

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(args.days) * 86400 * 1000
    t_start = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc).isoformat()
    t_end = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc).isoformat()
    print(f"Ventana UTC: {t_start} → {t_end} (~{args.days} días)", flush=True)

    export_base = Path(args.export_dir) if args.export_dir else None
    reports: list[dict[str, Any]] = []
    written_csv: list[str] = []

    last_report: dict[str, Any] | None = None
    for sym in syms:
        print(f"Descargando {sym} 5m …", flush=True)
        raw = fetch_klines_5m_full(sym, start_ms, end_ms)
        if not raw:
            raise SystemExit(f"No se obtuvieron velas para {sym}.")
        rows = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in raw]

        if export_base is not None:
            csv_path = export_base / f"{sym.lower()}_5m_{int(args.days)}d.csv"
            write_klines_csv(csv_path, sym, raw)
            rel = str(csv_path)
            written_csv.append(rel)
            print(f"  CSV → {csv_path} ({len(raw)} filas)", flush=True)

        t0 = datetime.fromtimestamp(rows[0][0] / 1000.0, tz=timezone.utc).isoformat()
        t1 = datetime.fromtimestamp(rows[-1][0] / 1000.0, tz=timezone.utc).isoformat()
        report: dict[str, Any] = {
            "symbol": sym,
            "interval": "5m",
            "first_open_time_utc": t0,
            "last_open_time_utc": t1,
            "days_requested": int(args.days),
            "summary": summarize(rows, args.range_pct_basis),
        }
        if args.by_hour:
            report["by_hour_utc"] = by_hour(rows, args.range_pct_basis)
        reports.append(report)
        last_report = report

    if len(reports) == 1:
        txt = json.dumps(reports[0], indent=2, ensure_ascii=False)
        print(txt)
    else:
        print(json.dumps({"symbols": [r["symbol"] for r in reports], "n_each": [r["summary"]["n_candles"] for r in reports]}, indent=2))

    if args.out_json and last_report is not None:
        with open(args.out_json, "w", encoding="utf-8") as f:
            f.write(json.dumps(last_report, indent=2, ensure_ascii=False))

    if args.stats_md:
        md_path = Path(args.stats_md)
        md_path.parent.mkdir(parents=True, exist_ok=True)
        rels = written_csv if written_csv else [f"(no CSV; ejecutar con --export-dir) {r['symbol']}" for r in reports]
        md_path.write_text(build_stats_markdown(reports, rels), encoding="utf-8")
        print(f"Markdown → {md_path}", flush=True)


if __name__ == "__main__":
    main()
