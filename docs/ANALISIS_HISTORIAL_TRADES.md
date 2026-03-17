# Análisis del historial de trades (15 mar 2026)

## Resumen del día

| Métrica | Valor |
|--------|--------|
| **Total de trades** | 166 |
| **Suma invertida** | **$946.75** |
| **Lado YES** | 111 trades · $509.25 |
| **Lado NO** | 55 trades · $437.50 |
| **Primer trade** | 08:19 UTC |
| **Último trade** | 21:08 UTC |

Esa suma es **cuánto se ha puesto en juego** (coste de las posiciones). El PnL real depende de **cuántos mercados resolvieron a favor o en contra**. Si resolvieron ~50% a favor, estarías cerca del break-even; si menos, hay pérdida.

---

## Limitación actual

En `trades_history.json` **no guardamos el resultado** (si el mercado cerró YES o NO). Solo tenemos: timestamp, market, side, price, investment, prob_mc, order_id. Por tanto **no se puede calcular win rate ni PnL exacto** solo con ese archivo.

Para saber si vamos ganando o perdiendo hace falta:

1. **Consultar Polymarket** (web o API) por posiciones cerradas y redimidas, o  
2. **Añadir registro de resolución** en el bot: cuando un mercado que operamos pase a `closed=true`, consultar el outcome y apuntar en un `resolutions.json` o en el mismo historial si ganamos o perdimos.

---

## Recomendaciones para frenar pérdidas

### 1. Ser más selectivo (subir el edge mínimo)

Ahora mismo `MIN_EDGE_REQUIRED = 0.07` (7%). Con 166 trades en un día, se está entrando en **casi todo** lo que supera 7%. Si el modelo está algo mal calibrado o el mercado es eficiente, ese “edge” puede ser falso y se pierde por comisiones y por perder más del 50% de las veces.

**Cambio recomendado:** subir a **10% o 12%** para entrar solo en señales más claras.

- En el código: `MIN_EDGE_REQUIRED = 0.10` (o `0.12`).
- Efecto esperado: menos trades por día, solo los de mayor convicción. Si el edge real existe, se mantiene beneficio con menos riesgo; si no, se pierde menos.

### 2. Reducir tamaño por trade (opcional)

Seguir con máx. **$10** y **10 shares** está bien. Si quieres ser más conservador mientras revisas, se puede bajar a **$5** por operación (cambiar `MAX_TRADE_USD = 5.00`).

### 3. Registrar resolución para medir de verdad

Sin datos de resolución no se puede saber si el bot es rentable. Próximo paso útil:

- Un script (o tarea periódica) que:
  - Lea `trades_history.json` (por ejemplo trades de las últimas 24–48 h).
  - Para cada `market_id` (o condition_id), consulte Gamma/CLOB para ver si el mercado está `closed` y cuál fue el outcome.
  - Apunte en un archivo (p. ej. `resolutions.json` o columnas en CSV): `market_id`, `side_apostado`, `outcome_real`, `investment`, `pnl`.
- Con eso se calcula: win rate, PnL total, y si conviene subir/bajar el edge o el tamaño.

---

## Cómo comprobar tu PnL a mano (Polymarket)

1. Entra en [Polymarket](https://polymarket.com) con la wallet del proxy.
2. Ve a **Portfolio** o **Activity** y filtra por **Resolved** / **Closed**.
3. Ahí ves posiciones ya resueltas: si tu lado ganó, recuperaste $1 por share; si perdió, $0.
4. Compara el **balance actual** con el que tenías al inicio del día (o con el de ayer) para tener una idea de PnL real.

---

## Resumen ejecutivo

- **166 trades y ~$947 invertidos en un día** es mucho volumen; con edge mínimo 7% es fácil estar sobreoperando.
- **No tenemos resolución en el log** → no podemos calcular win rate ni PnL solo con el historial actual.
- **Recomendación inmediata:** subir `MIN_EDGE_REQUIRED` a **0.10** (o 0.12) para reducir operaciones y concentrarte en señales más fuertes.
- **Siguiente paso:** implementar registro de resolución (script o integración en el bot) para medir PnL y calibrar el edge con datos reales.
