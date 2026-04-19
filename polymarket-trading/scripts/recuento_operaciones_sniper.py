#!/usr/bin/env python3
"""
Recuento de operaciones de la estrategia Monte Carlo Sniper.
Lee ~/trades_history.json (o TRADES_HISTORY_FILE) y muestra:
total operaciones, ganadoras/perdedoras, PnL, por activo, rango temporal, últimas N.
"""
import os
import json
from pathlib import Path
from datetime import datetime, timezone

TRADES_FILE = os.path.expanduser(os.getenv("TRADES_HISTORY_FILE", "~/trades_history.json"))
# Filtrar solo operaciones del bot: shares en (0, MAX_SHARES]. Para incluir todo, usar MAX_SHARES=999 o no definir.
MAX_SHARES_FILTER = float(os.getenv("SNIPER_MAX_SHARES_FILTER", "2"))


def parse_ts(x):
    if x is None:
        return None
    if isinstance(x, (int, float)):
        t = float(x)
        if t > 1e12:
            t /= 1000.0
        return datetime.fromtimestamp(t, tz=timezone.utc)
    if isinstance(x, str):
        s = x.strip()
        try:
            t = float(s)
            if t > 1e12:
                t /= 1000.0
            return datetime.fromtimestamp(t, tz=timezone.utc)
        except ValueError:
            pass
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return None
    return None


def main():
    p = Path(TRADES_FILE)
    if not p.exists():
        print(f"No existe {TRADES_FILE}")
        return

    raw = json.loads(p.read_text())
    trades = raw if isinstance(raw, list) else raw.get("trades") or raw.get("history") or []

    def _shares(t):
        s = t.get("shares")
        if s is None:
            return None
        try:
            return float(s)
        except (TypeError, ValueError):
            return None

    rows = []
    for t in trades:
        if not isinstance(t, dict):
            continue
        if not t.get("resolved"):
            continue
        pnl = t.get("pnl")
        if not isinstance(pnl, (int, float)):
            continue
        ts = parse_ts(t.get("resolved_at") or t.get("timestamp"))
        if ts is None:
            continue
        sh = _shares(t)
        if sh is not None and not (0 < sh <= MAX_SHARES_FILTER):
            continue  # Excluir manuales (solo 0 < shares <= MAX)
        rows.append((ts, float(pnl), t))

    rows.sort(key=lambda x: x[0])

    if not rows:
        print("No hay operaciones resueltas con PnL en el historial.")
        return

    pnls = [r[1] for r in rows]
    wins = sum(1 for p in pnls if p > 1e-9)
    losses = sum(1 for p in pnls if p < -1e-9)
    be = len(pnls) - wins - losses
    total_pnl = sum(pnls)
    avg_pnl = total_pnl / len(pnls)
    winrate = wins / (wins + losses) if (wins + losses) > 0 else 0.0

    eq = 0.0
    peak = 0.0
    maxdd = 0.0
    for p in pnls:
        eq += p
        peak = max(peak, eq)
        maxdd = min(maxdd, eq - peak)

    print("=== RECUENTO OPERACIONES: MONTE CARLO SNIPER ===")
    print()
    print("Archivo:", TRADES_FILE)
    if MAX_SHARES_FILTER < 999:
        print("Filtro: solo trades con 0 < shares <=", MAX_SHARES_FILTER, "(excluye operaciones manuales)")
    print()
    print("Total operaciones (resueltas con PnL):", len(rows))
    print("  Ganadoras:", wins)
    print("  Perdedoras:", losses)
    print("  Break-even:", be)
    print("  Win rate:", round(100 * winrate, 1), "%")
    print()
    print("PnL total (USD):", round(total_pnl, 2))
    print("PnL promedio por operación:", round(avg_pnl, 3))
    print("Mayor drawdown (USD):", round(maxdd, 2))
    print()

    by_market = {}
    for ts, pnl, t in rows:
        m = (t.get("market") or "—").upper()
        by_market.setdefault(m, []).append((ts, pnl))
    print("--- Por activo ---")
    for m in sorted(by_market.keys()):
        data = by_market[m]
        pnls_m = [x[1] for x in data]
        w = sum(1 for x in pnls_m if x > 1e-9)
        l = sum(1 for x in pnls_m if x < -1e-9)
        tot = sum(pnls_m)
        print(f"  {m}: operaciones={len(data)} | ganadoras={w} | perdedoras={l} | PnL={round(tot, 2)} USD")

    print()
    print("--- Rango temporal ---")
    print("  Primera operación:", rows[0][0].strftime("%Y-%m-%d %H:%M UTC"))
    print("  Última operación:", rows[-1][0].strftime("%Y-%m-%d %H:%M UTC"))

    n_last = min(10, len(rows))
    print()
    print(f"--- Últimas {n_last} operaciones ---")
    for ts, pnl, t in rows[-n_last:]:
        m = t.get("market", "—")
        side = t.get("side", "—")
        inv = t.get("investment", 0)
        won = t.get("won", pnl > 0)
        print(f"  {ts.strftime('%Y-%m-%d %H:%M')} | {m} {side} | inv ${inv} | {'GANÓ' if won else 'PERDIÓ'} | PnL {pnl:+.2f}")


if __name__ == "__main__":
    main()
