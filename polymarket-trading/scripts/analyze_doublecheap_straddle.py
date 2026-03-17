import os
import json
from collections import defaultdict


IN_FILE = os.path.expanduser(os.getenv("OB_IN_FILE", "~/orderbook_snapshots.jsonl"))

# Umbrales a evaluar. Por defecto: 0.30–0.35 (inclusive) en pasos de 0.01.
# Puedes sobreescribir con OB_LIMITS="0.30,0.32,0.35" o similar.
_limits_env = os.getenv("OB_LIMITS", "").strip()
if _limits_env:
    LIMITS = [float(x.strip()) for x in _limits_env.split(",") if x.strip()]
else:
    LIMITS = [round(0.30 + i * 0.01, 2) for i in range(6)]  # 0.30..0.35

# Modo de sizing:
# - "usd": invierte USD_PER_LEG en cada pierna cuando se activa
# - "shares": compra SHARES_PER_LEG shares de cada pierna cuando se activa
MODE = os.getenv("OB_MODE", "usd").strip().lower()
USD_PER_LEG = float(os.getenv("OB_USD_PER_LEG", "1.0"))
SHARES_PER_LEG = float(os.getenv("OB_SHARES_PER_LEG", "1.0"))


def parse_lines(path: str):
    with open(path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def main():
    if not os.path.exists(IN_FILE):
        raise SystemExit(f"No existe {IN_FILE}")

    # Para cada market_id, guardamos la serie de asks para contar "touches"
    per = defaultdict(lambda: {
        "ticker": None,
        "question": None,
        "endDate": None,
        "rows": [],  # (ts, yes_ask, no_ask)
    })

    n_lines = 0
    for o in parse_lines(IN_FILE):
        n_lines += 1
        mid = str(o.get("market_id"))
        ts = o.get("ts")
        ya = o.get("yes_ask")
        na = o.get("no_ask")

        p = per[mid]
        p["ticker"] = p["ticker"] or o.get("ticker")
        p["question"] = p["question"] or o.get("question")
        p["endDate"] = p["endDate"] or o.get("endDate")
        p["rows"].append((ts, ya, na))

    markets = list(per.values())

    def _touch_stats(rows, limit):
        """Cuenta cuántas veces el ask 'entra' (cruza) a <=limit. También guarda primer touch y min."""
        first = None  # (ts, ask)
        touches = 0
        min_ask = None
        prev_in = None
        for ts, ask in rows:
            if not isinstance(ask, (int, float)):
                continue
            min_ask = ask if min_ask is None else min(min_ask, ask)
            inside = ask <= limit
            if prev_in is None:
                prev_in = inside
            # Contar "touch" como cruce de fuera->dentro
            if inside and (prev_in is False):
                touches += 1
                if first is None:
                    first = (ts, ask)
            # Si arranca ya dentro, cuenta como 1 touch también (orden habría estado llena al primer snapshot)
            if inside and prev_in is None:
                touches += 1
                if first is None:
                    first = (ts, ask)
            prev_in = inside
        # Caso: primer punto ya dentro
        if first is None:
            for ts, ask in rows:
                if isinstance(ask, (int, float)) and ask <= limit:
                    first = (ts, ask)
                    touches = max(touches, 1)
                    break
        return {"first": first, "touches": touches, "min": min_ask}

    def _pnl_theoretical(first_yes, first_no):
        """PnL teórico si llenan ambas piernas a esos asks (sin fees/slippage)."""
        py = first_yes[1]
        pn = first_no[1]
        if MODE == "shares":
            cost = SHARES_PER_LEG * py + SHARES_PER_LEG * pn
            return SHARES_PER_LEG - cost
        shares_yes = USD_PER_LEG / py if py > 0 else 0
        shares_no = USD_PER_LEG / pn if pn > 0 else 0
        return max(shares_yes, shares_no) - 2 * USD_PER_LEG

    print("=== DOUBLE-CHEAP STRADDLE (simulación 2 con snapshots reales) ===")
    print(f"Archivo: {IN_FILE}")
    print(f"Líneas leídas: {n_lines}")
    print(f"Umbrales evaluados: {', '.join(f'{x:.2f}' for x in LIMITS)}")
    print(f"Modo: {MODE} | usd_per_leg={USD_PER_LEG} | shares_per_leg={SHARES_PER_LEG}")
    print()
    print(f"Markets únicos: {len(markets)}")
    print()

    # Resumen por umbral
    for limit in LIMITS:
        hits_both = 0
        hits_one = 0
        pnl_hits = []
        orders = {"YES->NO": 0, "NO->YES": 0, None: 0}

        for m in markets:
            rows = m["rows"]
            yes_rows = [(ts, ya) for ts, ya, _ in rows]
            no_rows = [(ts, na) for ts, _, na in rows]
            ys = _touch_stats(yes_rows, limit)
            ns = _touch_stats(no_rows, limit)
            has_yes = ys["first"] is not None
            has_no = ns["first"] is not None
            if has_yes and has_no:
                hits_both += 1
                order = "YES->NO" if ys["first"][0] <= ns["first"][0] else "NO->YES"
                orders[order] += 1
                pnl_hits.append(_pnl_theoretical(ys["first"], ns["first"]))
            elif has_yes or has_no:
                hits_one += 1

        print(f"--- Umbral ask<= {limit:.2f} ---")
        print(f"  Hits 2 piernas: {hits_both} | Hits 1 pierna: {hits_one} | Hits 0: {len(markets)-hits_both-hits_one}")
        if markets:
            print(f"  Frecuencia 2 piernas: {100.0*hits_both/len(markets):.1f}% | 1 pierna: {100.0*hits_one/len(markets):.1f}%")
        print(f"  Orden (solo 2 piernas): YES->NO={orders['YES->NO']} | NO->YES={orders['NO->YES']}")
        if pnl_hits:
            avg = sum(pnl_hits) / len(pnl_hits)
            med = sorted(pnl_hits)[len(pnl_hits)//2]
            print(f"  PnL teórico (sin fees) sobre 2 piernas: promedio {avg:+.3f} USD | mediana {med:+.3f} USD")
        else:
            print("  PnL teórico: n/a (sin 2 piernas aún)")
        print()

    # Mostrar detalle de los markets más “cercanos” al rango, con toques
    limit_hi = max(LIMITS) if LIMITS else 0.35
    scored = []
    for m in markets:
        rows = m["rows"]
        yes_rows = [(ts, ya) for ts, ya, _ in rows]
        no_rows = [(ts, na) for ts, _, na in rows]
        ys = _touch_stats(yes_rows, limit_hi)
        ns = _touch_stats(no_rows, limit_hi)
        a = ys["min"] if ys["min"] is not None else 1.0
        b = ns["min"] if ns["min"] is not None else 1.0
        scored.append((min(a, b), m, ys, ns))
    scored.sort(key=lambda x: x[0])

    print(f"Detalle (top 12 por asks mínimos; umbral_ref={limit_hi:.2f}):")
    for _, m, ys, ns in scored[:12]:
        def fmt_first(x):
            return f"{x[1]:.2f}@{str(x[0])[:19]}" if x else "—"
        print(
            f"- {m.get('ticker') or '—'} | min_yes={ys['min']} min_no={ns['min']} | touches_yes={ys['touches']} touches_no={ns['touches']} | first_yes={fmt_first(ys['first'])} first_no={fmt_first(ns['first'])} | {str(m.get('question') or '')[:60]}"
        )


if __name__ == "__main__":
    main()

