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

## 3. Resultados de las simulaciones

Última corrida: **228.777 snapshots**, **304 markets únicos** (EC2).

### 3.1 Tabla por umbral

| Umbral | Hits 2 piernas | Hits 1 pierna | Frecuencia 2p | Orden YES→NO / NO→YES | PnL medio (2p) | Hit 1 pierna: % a favor |
|--------|----------------|---------------|---------------|------------------------|----------------|--------------------------|
| 0.30 | 51 | 238 | 16.8% | 25 / 26 | +1.75 USD | **15.7%** |
| 0.31 | 55 | 235 | 18.1% | 26 / 29 | +1.68 USD | 15.9% |
| 0.32 | 62 | 229 | 20.4% | 27 / 35 | +1.50 USD | 15.0% |
| 0.33 | 67 | 225 | 22.0% | 31 / 36 | +1.37 USD | 15.2% |
| 0.34 | 72 | 223 | 23.7% | 31 / 41 | +1.26 USD | 15.8% |
| 0.35 | 80 | 217 | 26.3% | 35 / 45 | +1.16 USD | 16.3% |

- **Hit 1 pierna: % a favor** = de los markets donde solo una pierna llegó al umbral (solo compramos YES barato o solo NO barato), **qué % resolvió a favor** de esa pierna (es decir, la pierna que compramos ganó). Fuente: Gamma API `GET /markets/{id}` → `outcomePrices` una vez el mercado está cerrado.

### 3.2 Interpretación

- **Umbral más estricto (0.30):** menos oportunidades (~17%) pero mayor PnL por trade (~+1.75 USD).
- **Umbral más relajado (0.35):** más oportunidades (~26%) y PnL por trade algo menor (~+1.16 USD).
- El **orden** de activación está repartido; NO→YES suele ser un poco más frecuente que YES→NO.
- **Hit 1 pierna:** en ~78% de los markets solo una pierna toca el umbral. De esos, **solo ~15–16% resolvieron a favor** de la pierna que habríamos comprado; el resto (~84%) resolvieron en contra. Es decir, cuando solo se llena una pierna, esa pierna suele ser la perdedora: el mercado la puso barata porque la probabilidad real era baja.

*Nota:* PnL es teórico (sin fees ni slippage). La ejecución real con órdenes límite puede no llenar siempre al precio del primer touch.

### 3.3 PnL si dejamos “hit 1” hasta resolución

Si **no** cancelamos/hedgeamos y dejamos cada pierna suelta (solo una llenada) hasta que el mercado resuelve, asumiendo **1 USD por pierna** en cada hit 1:

| Umbral | PnL ganadas (hit 1) | PnL perdidas (hit 1) | **TOTAL (hit 1)** |
|--------|---------------------|----------------------|-------------------|
| 0.30   | +96.99              | −206.00              | **−109.01 USD**   |
| 0.31   | +91.55              | −203.00              | **−111.45 USD**   |
| 0.32   | +81.48              | −200.00              | **−118.52 USD**   |
| 0.33   | +79.01              | −196.00              | **−116.99 USD**   |
| 0.34   | +76.63              | −193.00              | **−116.37 USD**   |
| 0.35   | +72.96              | −187.00              | **−114.04 USD**   |

**Conclusión:** En todos los umbrales, el PnL neto de “dejar hit 1 hasta resolución” es **negativo** (aprox. −109 a −118 USD en el periodo analizado). Las ganancias de los ~15–16% que resuelven a favor no compensan las pérdidas del ~84% que resuelven en contra. Por tanto **no** conviene dejar la pierna suelta hasta resolución; hay que aplicar regla de cancelación, hedge o timeout (ver §4).

### 3.4 Descubrimiento: reglas para “hit 1” que mejoran el PnL

En una corrida reciente sobre el histórico **que se está registrando** en `~/orderbook_snapshots.jsonl` (EC2), se simuló el manejo del caso “hit 1” con sizing **1 USD por pierna** (sin fees).

#### Opción 1: timeout + cancelar la otra orden + salir al bid

- **Regla:** si se llena una pierna, esperar \(T\) segundos a que la otra llegue al umbral. Si no llega, **cancelar** la orden restante y **salir** de la pierna llenada vendiendo al **bid** en/tras el timeout.
- **Parámetros simulados:** \(T = 45s\), salida = `bid@timeout`.
- **Resultado:** convierte el “hit 1” (muy negativo a resolución) en un coste pequeño y mantiene el PnL total cerca de break-even / ligeramente positivo (según umbral).

#### Opción 2: filtro “solo entrar cuando la 2ª pierna se acerca” + misma salida por timeout

Para evitar entrar en la mayoría de hits‑1 (que tienden a perder), se aplicó un filtro:

- **Filtro:** tras el primer “cheap” (una pierna toca el umbral), solo “disparar” el trade si dentro de `confirm` la pierna opuesta baja a **ask ≤ (umbral + other_within)**. Recién ahí se coloca el straddle (y se mantiene el timeout/exit de la opción 1).
- **Parámetros simulados:** `confirm=60s`, `other_within=+0.02` (ask), `timeout=45s`, salida = `bid@timeout`.
- **Resultado:** menos trades (menos “triggers”), pero con una tasa de conversión a 2 piernas muy alta; el PnL total mejora fuertemente.

**Ejemplo (umbral 0.35, opción 2):** TOTAL **+38.17 USD** (en el histórico analizado; \(1 USD\) por pierna, sin fees).  
**Desglose por activo (opción 2, umbral 0.35):**

| Activo | triggers | conv a 2 piernas | stops 1 pierna | TOTAL |
|--------|----------|------------------|----------------|-------|
| BTC | 18 | 16 | 2 | **+20.37 USD** |
| ETH | 16 | 14 | 2 | **+17.79 USD** |

---

## 4. Recomendación: ¿es viable el straddle?

- **Sí, con condiciones.** Cuando **se llenan las dos piernas** (hits 2 piernas), el PnL teórico por trade es claramente positivo (+1.16 a +1.75 USD según umbral, sin fees). La frecuencia (17–26% de los markets) es suficiente para plantear un bot que coloque órdenes límite en ambas direcciones.
- **Regla obligatoria para “solo una pierna”:** en los casos en que solo se llena una orden (la mayoría: ~78% de los markets con oportunidad), esa pierna **pierde ~84% de las veces**. Por tanto:
  - **No** conviene dejar la pierna suelta hasta resolución como apuesta direccional.
  - Hay que definir una regla explícita: por ejemplo **cancelar la otra orden** tras X minutos si solo se llenó una, o **hedge** en el libro (comprar la otra pierna a precio de mercado, asumiendo coste), o **timeout** y asumir la pérdida de la pierna llenada como coste de la estrategia. Sin esa regla, el straddle se degrada por el gran número de “hit 1 pierna” que resuelven en contra.
- **Umbral sugerido:** 0.30–0.32 si se prioriza PnL por trade; 0.33–0.35 si se prioriza más frecuencia. Órdenes **límite** (no mercado) para controlar el precio de entrada.

---

## 5. Cómo seguir la investigación

- **Captura continua:** dejar el recorder activo (p. ej. `pumaclaw-orderbook-recorder.service`) para acumular más markets y repetir el análisis periódicamente.
- **Umbrales:** probar otros rangos vía `OB_LIMITS` (ej. `0.28,0.30,0.35,0.40`).
- **Regla "solo una pierna":** los datos muestran que ~84% de esos casos resuelven en contra; conviene cancelar/hedge/timeout (ver §4) y no mantener la pierna suelta hasta resolución.
- **Fees y slippage:** en una siguiente iteración se puede restar un coste por trade o usar mid/spread del libro para acotar el PnL realista.

---

## 6. Relación con el resto del proyecto

- **Monte Carlo Sniper** (`montecarlo_sniper.py`): estrategia distinta — usa GBM para estimar probabilidad YES/NO y opera cuando hay edge vs el libro (una sola pierna por señal). Ver [VIABILIDAD_ESTRATEGIA_MONTECARLO.md](VIABILIDAD_ESTRATEGIA_MONTECARLO.md).
- **Double-cheap straddle:** no usa GBM; usa **solo datos reales del orderbook** para medir frecuencia y PnL teórico de comprar ambas piernas baratas. Complementa la investigación y puede servir como base para un bot alternativo o híbrido una vez definidas las reglas de ejecución y de "una pierna".

---

## 7. Referencia rápida de scripts y servicios

| Componente | Ruta | Descripción |
|-----------|------|-------------|
| Recolector | `polymarket-trading/scripts/orderbook_recorder.py` | Graba snapshots del libro (YES/NO) en JSONL |
| Analizador | `polymarket-trading/scripts/analyze_doublecheap_straddle.py` | Lee JSONL, reporta hits 0/1/2 piernas, orden y PnL teórico |
| Servicio recorder | `polymarket-trading/pumaclaw-orderbook-recorder.service` | systemd para correr el recorder 24/7 |
| Salida por defecto | `~/orderbook_snapshots.jsonl` | Archivo JSONL en el home del usuario que corre el recorder |
