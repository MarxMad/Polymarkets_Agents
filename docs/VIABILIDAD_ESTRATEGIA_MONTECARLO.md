# Análisis de viabilidad: Monte Carlo Sniper (Polymarket 5M)

**Nombre oficial de la estrategia:** **Monte Carlo Sniper**  
Ficha completa: [ESTRATEGIA_MONTECARLO_SNIPER.md](ESTRATEGIA_MONTECARLO_SNIPER.md).

---

## Resumen ejecutivo

| Aspecto | Valoración | Comentario |
|---------|------------|------------|
| **Alineación de datos** | ✅ Fuerte | Polymarket resuelve con Binance BTCUSDT/ETHUSDT (close 1m). Tu modelo usa Binance → sin basis risk de fuente. |
| **Modelo (GBM)** | ⚠️ Moderado | Estándar para corto plazo; subestima colas gordas y saltos. |
| **Edge sostenible** | ⚠️ Incierto | Depende de que el mercado esté mal valorado vs tu simulación; puede haber competencia y arbitraje. |
| **Ejecución y costes** | ✅ Controlado | Tamaño pequeño, cooldown, 1 trade/ciclo; fees y slippage acotados. |
| **Riesgo operativo** | ✅ Bajo | Servicio systemd, logs, Telegram; límites de tamaño y exposición. |

**Veredicto:** Estrategia **viable con condiciones**. Tiene base lógica sólida (mismo activo y fuente de resolución que el mercado) y gestión de riesgo razonable. La viabilidad económica real depende de backtests/forward con datos históricos y de que el edge no se erosione por más competencia.

---

## 1. Coherencia del modelo con el mercado

- **Resolución Polymarket 5M:** Los mercados del tag 102892 (5M) se resuelven con el **precio de cierre de la vela de 1 minuto** de Binance (BTCUSDT / ETHUSDT) en el instante de expiración.
- **Tu pipeline:** Precio actual y volatilidad (y drift) desde Binance, ventana 7–20 min hasta el cierre.
- **Conclusión:** No hay desalineación de fuente (no estás modelando Binance contra un mercado que resuelva con CME u otro exchange). Eso es un **punto fuerte** para viabilidad: el “precio a batir” y tu simulación hablan del mismo proceso.

---

## 2. Fortalezas del diseño

1. **Monte Carlo con GBM**  
   Para horizontes de 10–20 minutos, GBM es un estándar razonable: volatilidad y drift recientes (60 velas 1m) capturan régimen reciente. No es un modelo exótico ni frágil por definición.

2. **Ventana 7–20 min**  
   Evitar los últimos 7 min reduce ruido de microestructura y posibles manipulaciones de último minuto; el límite superior evita mercados con demasiada incertidumbre. Criterio sensato.

3. **Edge mínimo 7% y ask < 0.85**  
   Exiges una desviación clara entre tu probabilidad y el precio; el tope en 0.85 evita apostar a cuotas muy altas donde el edge suele ser más ilusorio.

4. **Kelly fraccional (50%) + cap 5% bankroll + $10/trade**  
   Controlas exposición y no apuestas el Kelly completo; con bankroll del orden de cientos de dólares, el riesgo por operación está acotado.

5. **Un trade por ciclo + cooldown 30 s**  
   Limita sobreoperación y concentración de riesgo en pocos segundos.

6. **Memoria TRADED_MARKETS**  
   No repites el mismo mercado; evita doble exposición y “revenge trading” en el mismo evento.

7. **Auditoría (log + Telegram + trades_history.json)**  
   Permite medir si el edge real ex post coincide con el esperado.

---

## 3. Debilidades y riesgos

1. **GBM y colas gordas**  
   En cripto, las colas son más gordas que la normal. En eventos binarios de corto plazo, un solo movimiento brusco puede liquidar muchas simulaciones. Tu probabilidad puede ser demasiado “suave” frente a saltos reales → **riesgo:** sobrestimar la precisión del modelo en días volátiles.

2. **Volatilidad histórica reciente (60 × 1m)**  
   Si la volatilidad sube o baja justo antes del cierre, la ventana de 60 min puede quedar desfasada. No es invalidante, pero puede restar edge en regímenes de cambio de volatilidad.

3. **Origen del edge**  
   El edge existe solo si el mercado (el libro) valora distinto a tu simulación. Posibles explicaciones:
   - Mercado menos sofisticado (retail) → edge puede persistir un tiempo.
   - Otros bots con modelos similares → el edge se arbitra y se reduce.
   - Tu modelo está mal calibrado (p.ej. volatilidad sistemáticamente alta/baja) → edge ilusorio o negativo.
   Sin datos históricos de resolución vs predicción, **no se puede afirmar que el edge sea sostenible**, solo que el diseño es coherente.

4. **Toma de liquidez (market taker)**  
   Compras al ask. En libros finos puede haber deslizamiento y reacción del market maker; con tamaño pequeño ($10) el impacto suele ser limitado, pero en mercados muy ilíquidos el coste real puede ser mayor que el teórico.

5. **priceToBeat = 0**  
   Si `priceToBeat` viene 0 y usas `current_px` como target, en la práctica estás preguntando “precio por encima del precio actual”, que en GBM simétrico ronda 50%. Casi nunca dará edge >7%; está bien como fallback pero no genera oportunidades por sí solo. Dependes de que la API entregue un `priceToBeat` no nulo en la mayoría de los eventos.

6. **Liquidez mínima (5 unidades)**  
   El ajuste a 5 shares puede hacer que en algún mercado inviertas un poco más de lo que Kelly sugiere; es un efecto menor pero existe.

---

## 4. Qué falta para cuantificar viabilidad

1. **Backtest con resolución real**  
   Para cada mercado histórico (event id, endDate, priceToBeat, token ids):
   - Guardar tu `prob_yes`/`prob_no` y el ask al que habrías entrado.
   - Tras la resolución, ver si YES/NO ganó.
   - Calcular PnL teórico (sin ejecución) y comparar con “estrategia aleatoria” o “siempre al mid”.
   Sin esto, la viabilidad es **cualitativa**, no numérica.

2. **Forward test / paper trading**  
   Registrar durante 1–2 semanas: cada señal que habrías tomado, el fill simulado (ask) y el resultado al resolver. Así ves si el edge se materializa en tiempo real (incluyendo disponibilidad de liquidez y calidad del libro).

3. **Análisis de calibración**  
   Cuando tengas muchas resoluciones: comparar tu `prob_yes` con la frecuencia real de YES. Si las probabilidades están bien calibradas, las curvas de calibración (predicted prob vs realized freq) deberían estar cerca de la diagonal.

---

## 5. Conclusión y recomendaciones

- **Viabilidad técnica y de diseño:** **Sí.** La estrategia está bien alineada con la resolución de Polymarket (Binance), el modelo es razonable para el horizonte, y la gestión de riesgo (tamaño, cooldown, un trade por ciclo, auditoría) es adecuada para un bot automático.

- **Viabilidad económica:** **Incierta hasta tener datos.** Es plausible que exista edge si el mercado 5M está ineficiente o menos modelado que tu GBM; también es posible que el edge sea bajo o negativo por calibración, competencia o costes de ejecución. No se puede dar por “viable” en $ sin backtest/forward.

**Recomendaciones:**

1. **Mantener el bot en producción con tamaño pequeño** (como ahora) y **registrar todas las señales y resultados** (trades_history + resolución posterior) para construir una base de backtest/forward.
2. **Añadir un módulo de backtest** que, con historial de eventos 5M y de Binance, reproduzca `monte_carlo_probability` y compare con el ask del momento (o con el mid) y con el resultado real.
3. **Revisar eventos con `priceToBeat == 0`** en la API; si son mayoría, considerar filtrarlos o usar otra fuente de “strike” para no operar en condiciones casi 50/50.
4. **Opcional:** Probar una volatilidad ligeramente escalada (p.ej. 1.1× o 1.2× la histórica) en el GBM para ver si mejora la calibración en días de colas gordas; esto debe validarse con datos.

Con esto, tienes un marco claro para pasar de “estrategia bien diseñada y viable en principio” a “estrategia con viabilidad cuantificada y mejoras iterativas”.
