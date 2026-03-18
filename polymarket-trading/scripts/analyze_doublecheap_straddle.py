import os
import json
import time
from datetime import datetime, timezone
from collections import defaultdict

import requests

GAMMA_API = "https://gamma-api.polymarket.com"
IN_FILE = os.path.expanduser(os.getenv("OB_IN_FILE", "~/orderbook_snapshots.jsonl"))

# Simulación de gestión del "hit 1":
# Si se llena una pierna, esperamos HIT1_TIMEOUT_SEC a que se llene la otra.
# Si no se llena, salimos de la pierna llenada vendiendo al bid en/tras el timeout.
HIT1_TIMEOUT_SEC = float(os.getenv("OB_HIT1_TIMEOUT_SEC", "45"))

# Simulación opción 2: filtro para evitar entrar en "hit 1".
# Solo empezamos a tradear cuando vemos señal de que la 2ª pierna "se acerca":
# dentro de HIT2_CONFIRM_SEC desde el primer cheap, la otra pierna debe alcanzar
# ask <= (limit + HIT2_OTHER_WITHIN). Recién ahí colocamos ambas órdenes a "limit".
HIT2_CONFIRM_SEC = float(os.getenv("OB_HIT2_CONFIRM_SEC", "60"))
HIT2_OTHER_WITHIN = float(os.getenv("OB_HIT2_OTHER_WITHIN", "0.02"))

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


def ts_to_num(ts) -> float | None:
    """Convierte ts (epoch/ISO) a segundos (float) para ordenar y calcular ventanas."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        # Epoch como string
        try:
            return float(s)
        except Exception:
            pass
        # ISO 8601 (ej: 2026-03-17T17:36:24.123Z)
        try:
            if s.endswith("Z"):
                s2 = s[:-1] + "+00:00"
            else:
                s2 = s
            dt = datetime.fromisoformat(s2)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None
    return None


def fetch_resolution(market_id: str, cache: dict) -> bool | None:
    """True = YES ganó, False = NO ganó, None = mercado no cerrado o error."""
    if market_id in cache:
        return cache[market_id]
    try:
        r = requests.get(f"{GAMMA_API}/markets/{market_id}", timeout=5)
        if not r.ok:
            cache[market_id] = None
            return None
        m = r.json()
        if not m.get("closed"):
            cache[market_id] = None
            return None
        prices = json.loads(m.get("outcomePrices", "[]"))
        if len(prices) < 2:
            cache[market_id] = None
            return None
        yes_won = prices[0] == "1" or float(prices[0]) > 0.99
        cache[market_id] = yes_won
        return yes_won
    except Exception:
        cache[market_id] = None
        return None


def main():
    if not os.path.exists(IN_FILE):
        raise SystemExit(f"No existe {IN_FILE}")

    # Para cada market_id, guardamos la serie de asks para contar "touches"
    per = defaultdict(lambda: {
        "ticker": None,
        "question": None,
        "endDate": None,
        "rows": [],  # (ts_num, yes_ask, no_ask, yes_bid, no_bid)
    })

    n_lines = 0
    for o in parse_lines(IN_FILE):
        n_lines += 1
        mid = str(o.get("market_id"))
        ts = ts_to_num(o.get("ts"))
        ya = o.get("yes_ask")
        na = o.get("no_ask")
        yb = o.get("yes_bid")
        nb = o.get("no_bid")

        p = per[mid]
        p["market_id"] = mid
        p["ticker"] = p["ticker"] or o.get("ticker")
        p["question"] = p["question"] or o.get("question")
        p["endDate"] = p["endDate"] or o.get("endDate")
        if ts is not None:
            p["rows"].append((ts, ya, na, yb, nb))

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

    def _first_touch(rows_ask, limit):
        """Devuelve (ts, ask) del primer snapshot con ask<=limit; o None."""
        for ts, ask in rows_ask:
            if isinstance(ask, (int, float)) and ask <= limit:
                return (ts, ask)
        return None

    def _first_touch_within(rows_ask, limit, ts_start, ts_end):
        """Devuelve (ts, ask) del primer snapshot con ask<=limit en [ts_start, ts_end]."""
        for ts, ask in rows_ask:
            if ts is None:
                continue
            if ts < ts_start:
                continue
            if ts > ts_end:
                break
            if isinstance(ask, (int, float)) and ask <= limit:
                return (ts, ask)
        return None

    def _first_below_within(rows_ask, threshold, ts_start, ts_end):
        """Devuelve (ts, ask) del primer snapshot con ask<=threshold en [ts_start, ts_end]."""
        for ts, ask in rows_ask:
            if ts is None:
                continue
            if ts < ts_start:
                continue
            if ts > ts_end:
                break
            if isinstance(ask, (int, float)) and ask <= threshold:
                return (ts, ask)
        return None

    def _bid_at_or_after(rows_bid, ts_target):
        """Devuelve el primer bid (ts, bid) con ts>=ts_target; o None."""
        for ts, bid in rows_bid:
            if ts is None:
                continue
            if ts < ts_target:
                continue
            if isinstance(bid, (int, float)):
                return (ts, bid)
        return None

    def _pnl_one_leg_exit(price_buy, price_sell):
        """PnL de una pierna comprada y luego vendida (sin fees)."""
        if MODE == "shares":
            # Si compras SHARES_PER_LEG a price_buy y vendes al price_sell.
            return SHARES_PER_LEG * (price_sell - price_buy)
        if not price_buy or price_buy <= 0:
            return 0.0
        shares = USD_PER_LEG / price_buy
        return shares * price_sell - USD_PER_LEG

    print("=== DOUBLE-CHEAP STRADDLE (simulación 2 con snapshots reales) ===")
    print(f"Archivo: {IN_FILE}")
    print(f"Líneas leídas: {n_lines}")
    print(f"Umbrales evaluados: {', '.join(f'{x:.2f}' for x in LIMITS)}")
    print(f"Modo: {MODE} | usd_per_leg={USD_PER_LEG} | shares_per_leg={SHARES_PER_LEG}")
    print(f"Sim hit-1: timeout={HIT1_TIMEOUT_SEC:.0f}s | salida=bid@timeout (sin fees)")
    print(f"Sim opción2 filtro: confirm={HIT2_CONFIRM_SEC:.0f}s | other_within=+{HIT2_OTHER_WITHIN:.2f} (ask)")
    print()
    print(f"Markets únicos: {len(markets)}")
    print()

    resolution_cache = {}

    # Resumen por umbral
    for limit in LIMITS:
        hits_both = 0
        hits_one = 0
        one_leg_list = []  # (m, only_yes_cheap, only_no_cheap)
        pnl_hits = []
        orders = {"YES->NO": 0, "NO->YES": 0, None: 0}

        # Simulación opción 1 (timeout + cancel + salida):
        # - Si 2ª pierna entra <=limit dentro del timeout → tratamos como straddle (pnl teórico).
        # - Si no entra → salimos de la 1ª pierna al bid en/tras timeout y contamos ese pnl.
        sim_convert_to_both = 0
        sim_stopped_one_leg = 0
        sim_pnl_stops = []
        sim_pnl_converted = []

        # Simulación opción 2 (filtro): solo entramos si la otra pierna se acerca primero.
        sim2_triggers = 0
        sim2_convert_to_both = 0
        sim2_stopped_one_leg = 0
        sim2_pnl_stops = []
        sim2_pnl_converted = []

        # Desglose por activo (ticker) para opción 2
        sim2_by_ticker = defaultdict(lambda: {
            "triggers": 0,
            "conv": 0,
            "stops": 0,
            "pnl_conv": 0.0,
            "pnl_stop": 0.0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
        })

        for m in markets:
            ticker = (m.get("ticker") or "—").upper()
            rows = m["rows"]
            # Garantizar orden temporal
            rows_sorted = sorted(rows, key=lambda r: (r[0] is None, r[0]))
            yes_rows = [(ts, ya) for ts, ya, _, _, _ in rows_sorted]
            no_rows = [(ts, na) for ts, _, na, _, _ in rows_sorted]
            yes_bids = [(ts, yb) for ts, _, _, yb, _ in rows_sorted]
            no_bids = [(ts, nb) for ts, _, _, _, nb in rows_sorted]
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
                # Precio al que habríamos comprado la pierna barata (primer touch)
                price_buy = (ys["first"][1] if has_yes else ns["first"][1]) if (ys["first"] if has_yes else ns["first"]) else None
                one_leg_list.append((m, has_yes, has_no, price_buy))

            # Simulación del flujo de ejecución con timeout (independiente del conteo hits_* anterior).
            first_yes = _first_touch(yes_rows, limit)
            first_no = _first_touch(no_rows, limit)
            if not first_yes and not first_no:
                continue

            # Primera pierna llenada
            if first_yes and (not first_no or first_yes[0] <= first_no[0]):
                first_side = "YES"
                t0, p0 = first_yes
                other_touch = _first_touch_within(no_rows, limit, t0, t0 + HIT1_TIMEOUT_SEC)
                if other_touch:
                    sim_convert_to_both += 1
                    sim_pnl_converted.append(_pnl_theoretical(first_yes, other_touch))
                else:
                    # Stop: salir de YES al bid tras timeout
                    bid = _bid_at_or_after(yes_bids, t0 + HIT1_TIMEOUT_SEC)
                    if bid and isinstance(bid[1], (int, float)):
                        sim_stopped_one_leg += 1
                        sim_pnl_stops.append(_pnl_one_leg_exit(p0, bid[1]))

                # Opción 2 (filtro): solo "disparamos" si NO se acerca primero
                confirm = _first_below_within(
                    no_rows,
                    limit + HIT2_OTHER_WITHIN,
                    t0,
                    t0 + HIT2_CONFIRM_SEC,
                )
                if confirm:
                    sim2_triggers += 1
                    sim2_by_ticker[ticker]["triggers"] += 1
                    # Entramos en t_entry = confirm.ts, comprando YES a limit (ya estaba <=limit desde t0)
                    t_entry = confirm[0]
                    # ¿La otra pierna (NO) llega a <=limit dentro del timeout desde entrada?
                    other_touch2 = _first_touch_within(no_rows, limit, t_entry, t_entry + HIT1_TIMEOUT_SEC)
                    if other_touch2:
                        sim2_convert_to_both += 1
                        sim2_by_ticker[ticker]["conv"] += 1
                        _p = _pnl_theoretical(first_yes, other_touch2)
                        sim2_pnl_converted.append(_pnl_theoretical(first_yes, other_touch2))
                        sim2_by_ticker[ticker]["pnl_conv"] += _p
                        if _p > 1e-9:
                            sim2_by_ticker[ticker]["wins"] += 1
                        elif _p < -1e-9:
                            sim2_by_ticker[ticker]["losses"] += 1
                        else:
                            sim2_by_ticker[ticker]["breakeven"] += 1
                    else:
                        bid2 = _bid_at_or_after(yes_bids, t_entry + HIT1_TIMEOUT_SEC)
                        if bid2 and isinstance(bid2[1], (int, float)):
                            sim2_stopped_one_leg += 1
                            sim2_by_ticker[ticker]["stops"] += 1
                            _p = _pnl_one_leg_exit(p0, bid2[1])
                            sim2_pnl_stops.append(_pnl_one_leg_exit(p0, bid2[1]))
                            sim2_by_ticker[ticker]["pnl_stop"] += _p
                            if _p > 1e-9:
                                sim2_by_ticker[ticker]["wins"] += 1
                            elif _p < -1e-9:
                                sim2_by_ticker[ticker]["losses"] += 1
                            else:
                                sim2_by_ticker[ticker]["breakeven"] += 1
            else:
                first_side = "NO"
                t0, p0 = first_no
                other_touch = _first_touch_within(yes_rows, limit, t0, t0 + HIT1_TIMEOUT_SEC)
                if other_touch:
                    sim_convert_to_both += 1
                    sim_pnl_converted.append(_pnl_theoretical(other_touch, first_no))
                else:
                    bid = _bid_at_or_after(no_bids, t0 + HIT1_TIMEOUT_SEC)
                    if bid and isinstance(bid[1], (int, float)):
                        sim_stopped_one_leg += 1
                        sim_pnl_stops.append(_pnl_one_leg_exit(p0, bid[1]))

                confirm = _first_below_within(
                    yes_rows,
                    limit + HIT2_OTHER_WITHIN,
                    t0,
                    t0 + HIT2_CONFIRM_SEC,
                )
                if confirm:
                    sim2_triggers += 1
                    sim2_by_ticker[ticker]["triggers"] += 1
                    t_entry = confirm[0]
                    other_touch2 = _first_touch_within(yes_rows, limit, t_entry, t_entry + HIT1_TIMEOUT_SEC)
                    if other_touch2:
                        sim2_convert_to_both += 1
                        sim2_by_ticker[ticker]["conv"] += 1
                        _p = _pnl_theoretical(other_touch2, first_no)
                        sim2_pnl_converted.append(_pnl_theoretical(other_touch2, first_no))
                        sim2_by_ticker[ticker]["pnl_conv"] += _p
                        if _p > 1e-9:
                            sim2_by_ticker[ticker]["wins"] += 1
                        elif _p < -1e-9:
                            sim2_by_ticker[ticker]["losses"] += 1
                        else:
                            sim2_by_ticker[ticker]["breakeven"] += 1
                    else:
                        bid2 = _bid_at_or_after(no_bids, t_entry + HIT1_TIMEOUT_SEC)
                        if bid2 and isinstance(bid2[1], (int, float)):
                            sim2_stopped_one_leg += 1
                            sim2_by_ticker[ticker]["stops"] += 1
                            _p = _pnl_one_leg_exit(p0, bid2[1])
                            sim2_pnl_stops.append(_pnl_one_leg_exit(p0, bid2[1]))
                            sim2_by_ticker[ticker]["pnl_stop"] += _p
                            if _p > 1e-9:
                                sim2_by_ticker[ticker]["wins"] += 1
                            elif _p < -1e-9:
                                sim2_by_ticker[ticker]["losses"] += 1
                            else:
                                sim2_by_ticker[ticker]["breakeven"] += 1

        # Resolución de "hit 1 pierna": ¿cuántos resolvieron a favor? PnL si dejamos hasta resolución.
        resolved_favor = 0
        resolved_against = 0
        unresolved_count = 0
        pnl_one_leg_wins = 0.0
        pnl_one_leg_losses = 0.0
        for m, only_yes_cheap, only_no_cheap, price_buy in one_leg_list:
            yes_won = fetch_resolution(m.get("market_id", ""), resolution_cache)
            if yes_won is None:
                unresolved_count += 1
                continue
            time.sleep(0.03)
            leg_won = (only_yes_cheap and yes_won) or (only_no_cheap and not yes_won)
            if leg_won:
                resolved_favor += 1
                if price_buy and price_buy > 0:
                    # Ganamos: payout = (USD_PER_LEG/price_buy)*1, cost = USD_PER_LEG
                    pnl_one_leg_wins += USD_PER_LEG * (1.0 / price_buy - 1.0)
            else:
                resolved_against += 1
                pnl_one_leg_losses -= USD_PER_LEG

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

        # Resultados de la simulación con timeout (opción 1)
        sim_total_triggers = sim_convert_to_both + sim_stopped_one_leg
        if sim_total_triggers > 0:
            pnl_conv = sum(sim_pnl_converted) if sim_pnl_converted else 0.0
            pnl_stop = sum(sim_pnl_stops) if sim_pnl_stops else 0.0
            avg_stop = (pnl_stop / len(sim_pnl_stops)) if sim_pnl_stops else 0.0
            print(f"  Sim Opción1 (timeout {HIT1_TIMEOUT_SEC:.0f}s): conv_a_2p={sim_convert_to_both} | stops_1p={sim_stopped_one_leg} | PnL conv {pnl_conv:+.2f} | PnL stops {pnl_stop:+.2f} (avg/stop {avg_stop:+.3f}) → TOTAL {pnl_conv + pnl_stop:+.2f} USD")

        if sim2_triggers > 0:
            pnl2_conv = sum(sim2_pnl_converted) if sim2_pnl_converted else 0.0
            pnl2_stop = sum(sim2_pnl_stops) if sim2_pnl_stops else 0.0
            avg2_stop = (pnl2_stop / len(sim2_pnl_stops)) if sim2_pnl_stops else 0.0
            print(f"  Sim Opción2 (filtro): triggers={sim2_triggers} | conv_a_2p={sim2_convert_to_both} | stops_1p={sim2_stopped_one_leg} | PnL conv {pnl2_conv:+.2f} | PnL stops {pnl2_stop:+.2f} (avg/stop {avg2_stop:+.3f}) → TOTAL {pnl2_conv + pnl2_stop:+.2f} USD")

            # Tabla por activo (solo si hay BTC/ETH)
            keys = [k for k in sim2_by_ticker.keys() if k in ("BTC", "ETH")]
            if keys:
                print("    Por activo (Opción2):")
                for k in sorted(keys):
                    d = sim2_by_ticker[k]
                    total = d["pnl_conv"] + d["pnl_stop"]
                    print(f"      - {k}: triggers={d['triggers']} conv={d['conv']} stops={d['stops']} | wins={d['wins']} losses={d['losses']} be={d['breakeven']} → TOTAL {total:+.2f} USD (conv {d['pnl_conv']:+.2f} | stops {d['pnl_stop']:+.2f})")
        if hits_one > 0:
            total_resolved = resolved_favor + resolved_against
            pct_favor = 100.0 * resolved_favor / total_resolved if total_resolved else 0
            pnl_one_leg_total = pnl_one_leg_wins + pnl_one_leg_losses
            print(f"  Hit 1 pierna (solo una barata): resolvieron A FAVOR {resolved_favor} | en contra {resolved_against} | sin resolver {unresolved_count} → {pct_favor:.1f}% a favor")
            print(f"  Si dejamos hit 1 hasta resolución (${USD_PER_LEG}/pierna): PnL ganadas {pnl_one_leg_wins:+.2f} | PnL perdidas {pnl_one_leg_losses:+.2f} → TOTAL {pnl_one_leg_total:+.2f} USD")
        print()

    # Mostrar detalle de los markets más “cercanos” al rango, con toques
    limit_hi = max(LIMITS) if LIMITS else 0.35
    scored = []
    for m in markets:
        rows = m["rows"]
        yes_rows = [(ts, ya) for ts, ya, _, _, _ in rows]
        no_rows = [(ts, na) for ts, _, na, _, _ in rows]
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

