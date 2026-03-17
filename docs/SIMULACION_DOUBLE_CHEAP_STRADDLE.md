# Simulación 2: Double-Cheap Straddle (datos reales del orderbook)

Investigación sobre una estrategia alternativa en mercados binarios 5M de Polymarket: comprar **ambas piernas (YES y NO)** cuando el precio de cada una baja por debajo de un umbral (ej. 0.30–0.35). Al resolver, una pierna paga 1 USD por share y la otra 0; si ambas se compraron barato, el payoff puede ser positivo.

Este documento describe la idea, el pipeline de captura de datos reales, el script de análisis y los resultados de las simulaciones para que futuros builders puedan reproducir y extender la investigación.

---

## 1. Idea de la estrategia

- En mercados 5M "Up or Down", al inicio YES y NO suelen cotizar cerca de 0.50.
- Cuando el precio del subyacente se aleja del target, **una** de las dos se abarata (ej. precio sube → NO más barato).
- Si el precio **vuelve** y se cruza el target, la otra pierna puede abaratarse también en otro momento.
- **Hipótesis:** en una misma ventana de 5 minutos a veces se dan **dos** momentos en que YES ask ≤ X y NO ask ≤ X (en orden distinto). Si colocamos **órdenes límite** a X en ambas direcciones, podríamos llenar las dos a bajo coste; al resolver, una gana (~3.3× por share si X=0.30) y la otra pierde el coste → **PnL teórico positivo** si el coste total < payout.

**Ejemplo (conceptual):**  
- Comprar YES a 0.30 ($1 → 3.33 shares) y NO a 0.30 ($1 → 3.33 shares). Coste total $2.  
- Al resolver: una pierna paga 3.33 USD, la otra 0. PnL = 3.33 − 2 = **+1.33 USD** (sin fees).

La viabilidad depende de **con qué frecuencia** el libro real ofrece ambos asks ≤ umbral en el mismo mercado y de la **ejecución** (órdenes límite vs mercado, slippage, fees).

---

## 2. Pipeline: datos reales (simulación 2)

A diferencia del Monte Carlo del sniper (que simula caminos de precio con GBM), aquí usamos **snapshots reales del orderbook** del CLOB de Polymarket para medir si, en la práctica, se dan esas oportunidades.

### 2.1 Recolector: `orderbook_recorder.py`

- **Ubicación:** `polymarket-trading/scripts/orderbook_recorder.py`
- **Función:** cada N segundos (por defecto 1 s) consulta la API de Gamma por eventos 5M activos (tag 102892) y, para cada mercado BTC/ETH en ventana de tiempo configurable (ej. 1–20 min hasta cierre), pide el **order book** de los tokens YES y NO al CLOB. Guarda en un archivo **JSONL** una línea por snapshot con:
  - `ts`, `ticker`, `event_id`, `market_id`, `question`, `endDate`, `time_left_s`
  - `token_yes`, `token_no`
  - `yes_bid`, `yes_ask`, `no_bid`, `no_ask`
  - flags `cheap_yes` / `cheap_no` (ask ≤ umbral, por defecto 0.30)

**Variables de entorno (opcionales):**

| Variable | Descripción | Default |
|----------|-------------|---------|
| `OB_TAG_ID` | Tag Gamma (mercados 5M) | 102892 |
| `OB_LIMIT_PRICE` | Umbral "barato" para el flag | 0.30 |
| `OB_POLL_SECONDS` | Intervalo entre snapshots | 1.0 |
| `OB_WINDOW_MIN_SEC` / `OB_WINDOW_MAX_SEC` | Ventana de tiempo hasta cierre (segundos) | 60 – 1200 |
| `OB_OUT_FILE` | Ruta del JSONL de salida | `~/orderbook_snapshots.jsonl` |

**Servicio systemd (ejemplo en EC2):**  
`polymarket-trading/pumaclaw-orderbook-recorder.service` — deja el recorder corriendo 24/7 y escribiendo en `~/orderbook_snapshots.jsonl`.

### 2.2 Analizador: `analyze_doublecheap_straddle.py`

- **Ubicación:** `polymarket-trading/scripts/analyze_doublecheap_straddle.py`
- **Función:** lee el JSONL de snapshots y, por cada `market_id`:
  - Cuenta **cuántas veces** el ask de YES y el ask de NO **tocan** (cruzan) cada umbral (ej. 0.30, 0.31, …, 0.35).
  - Detecta si en algún momento **ambas** piernas estuvieron ≤ umbral (en cualquier orden temporal).
  - Clasifica cada market en: **Hits 0** (nunca barato), **Hits 1 pierna** (solo YES o solo NO barato), **Hits 2 piernas** (ambas baratas).
  - Para los hits de 2 piernas, calcula el **orden** (YES→NO o NO→YES según qué pierna tocó primero el umbral) y un **PnL teórico** asumiendo que se compró $1 (o N shares) en cada pierna al primer touch, sin fees ni slippage.

**Variables de entorno (opcionales):**

| Variable | Descripción | Default |
|----------|-------------|---------|
| `OB_IN_FILE` | Ruta del JSONL de snapshots | `~/orderbook_snapshots.jsonl` |
| `OB_LIMITS` | Umbrales separados por coma | (script usa 0.30–0.35 en pasos de 0.01) |
| `OB_MODE` | `usd` o `shares` | usd |
| `OB_USD_PER_LEG` / `OB_SHARES_PER_LEG` | Tamaño por pierna | 1.0 |

**Ejecución (en el servidor donde está el JSONL):**

```bash
# En EC2 (o donde corra el recorder)
/home/ubuntu/.openclaw/workspace/skills/polymarket/.venv/bin/python3 \
  /home/ubuntu/.openclaw/workspace/skills/polymarket/scripts/analyze_doublecheap_straddle.py
```

**Ejecución local** (con una copia del JSONL):

```bash
OB_IN_FILE=/ruta/a/orderbook_snapshots.jsonl python3 polymarket-trading/scripts/analyze_doublecheap_straddle.py
```

---

## 3. Resultados de las simulaciones (muestra)

Con **116.859 snapshots** y **158 markets únicos** (captura durante varias horas en EC2), el análisis dio (resumen):

| Umbral (ask ≤) | Hits 2 piernas | Hits 1 pierna | Hits 0 | Frecuencia 2 piernas | Orden YES→NO / NO→YES | PnL medio (2 piernas, sin fees) |
|----------------|----------------|---------------|--------|------------------------|------------------------|----------------------------------|
| 0.30 | 30 | 117 | 11 | 19.0% | 13 / 17 | +1.68 USD |
| 0.31 | 32 | 116 | 10 | 20.3% | 13 / 19 | +1.63 USD |
| 0.32 | 38 | 111 | 9 | 24.1% | 14 / 24 | +1.50 USD |
| 0.33 | 39 | 111 | 8 | 24.7% | 15 / 24 | +1.38 USD |
| 0.34 | 41 | 111 | 6 | 25.9% | 15 / 26 | +1.28 USD |
| 0.35 | 47 | 105 | 6 | 29.7% | 18 / 29 | +1.20 USD |

- **Umbral más estricto (0.30):** menos oportunidades (~19%) pero mayor PnL por trade (~+1.68 USD).
- **Umbral más relajado (0.35):** más oportunidades (~30%) y PnL por trade algo menor (~+1.20 USD).
- El **orden** de activación está repartido; NO→YES suele ser un poco más frecuente que YES→NO.
- En una gran parte de los markets **solo una pierna** llegó al umbral (≈74% con 0.30); ahí la estrategia requiere una regla de manejo (timeout, hedge o cierre) que aún no está definida.

*Nota:* PnL es teórico (sin fees ni slippage). La ejecución real con órdenes límite puede no llenar siempre al precio del primer touch.

---

## 4. Cómo seguir la investigación

- **Captura continua:** dejar el recorder activo (p. ej. `pumaclaw-orderbook-recorder.service`) para acumular más markets y repetir el análisis periódicamente.
- **Umbrales:** probar otros rangos vía `OB_LIMITS` (ej. `0.28,0.30,0.35,0.40`).
- **Regla "solo una pierna":** decidir qué hacer cuando solo se llena una orden límite (cancelar tras timeout, hedge en el libro, o mantener hasta resolución) y, si se implementa, incorporarla al análisis o a un bot de paper trading.
- **Fees y slippage:** en una siguiente iteración se puede restar un coste por trade o usar mid/spread del libro para acotar el PnL realista.

---

## 5. Relación con el resto del proyecto

- **Monte Carlo Sniper** (`montecarlo_sniper.py`): estrategia distinta — usa GBM para estimar probabilidad YES/NO y opera cuando hay edge vs el libro (una sola pierna por señal). Ver [VIABILIDAD_ESTRATEGIA_MONTECARLO.md](VIABILIDAD_ESTRATEGIA_MONTECARLO.md).
- **Double-cheap straddle:** no usa GBM; usa **solo datos reales del orderbook** para medir frecuencia y PnL teórico de comprar ambas piernas baratas. Complementa la investigación y puede servir como base para un bot alternativo o híbrido una vez definidas las reglas de ejecución y de "una pierna".

---

## 6. Referencia rápida de scripts y servicios

| Componente | Ruta | Descripción |
|-----------|------|-------------|
| Recolector | `polymarket-trading/scripts/orderbook_recorder.py` | Graba snapshots del libro (YES/NO) en JSONL |
| Analizador | `polymarket-trading/scripts/analyze_doublecheap_straddle.py` | Lee JSONL, reporta hits 0/1/2 piernas, orden y PnL teórico |
| Servicio recorder | `polymarket-trading/pumaclaw-orderbook-recorder.service` | systemd para correr el recorder 24/7 |
| Salida por defecto | `~/orderbook_snapshots.jsonl` | Archivo JSONL en el home del usuario que corre el recorder |
