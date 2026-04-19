# Documentación e investigación – PumaClaw / Polymarket

Este directorio contiene la documentación técnica y los análisis de viabilidad de las estrategias del proyecto. Sirve como base para futuros builders que quieran contribuir o extender la investigación.

---

## Índice de documentos

| Documento | Descripción |
|-----------|-------------|
| [ESTRATEGIA_MONTECARLO_SNIPER.md](ESTRATEGIA_MONTECARLO_SNIPER.md) | **Ficha de la estrategia en producción:** nombre oficial (**Monte Carlo Sniper**), descripción, flujo, parámetros, salidas y enlaces. |
| [VIABILIDAD_ESTRATEGIA_MONTECARLO.md](VIABILIDAD_ESTRATEGIA_MONTECARLO.md) | Análisis de viabilidad del Monte Carlo Sniper: alineación con resolución Polymarket/Binance, fortalezas/debilidades del GBM, edge y riesgo. |
| [ANALISIS_HISTORIAL_TRADES.md](ANALISIS_HISTORIAL_TRADES.md) | Análisis del historial de trades del sniper (volumen, PnL, patrones). |
| [RECUENTO_OPERACIONES_SNIPER.md](RECUENTO_OPERACIONES_SNIPER.md) | Recuento actualizado: total operaciones, por día, por activo (tablas en .md). |
| [SIMULACION_DOUBLE_CHEAP_STRADDLE.md](SIMULACION_DOUBLE_CHEAP_STRADDLE.md) | **Simulación 2** con datos reales del orderbook: estrategia "double-cheap straddle" (comprar YES y NO baratos), pipeline de captura, script de análisis y resultados (umbrales 0.30–0.35, hits 0/1/2 piernas, PnL teórico). |
| [STRADDLE_SNAPSHOT_BINANCE_BRIDGE.md](STRADDLE_SNAPSHOT_BINANCE_BRIDGE.md) | Puente straddle: snapshots + Binance, surrogate, simulación sintética; fases y comandos. |
| [BINANCE_5M_ULTIMOS_90D.md](BINANCE_5M_ULTIMOS_90D.md) | Velas Binance 5m (90 días): CSV en repo, resumen OHLC y cierres arriba/abajo del open. |
| [BINANCE_5M_90D_RACHAS_Y_VOL_HORA.md](BINANCE_5M_90D_RACHAS_Y_VOL_HORA.md) | Rachas consecutivas verde/rojo y volatilidad intravela por **hora UTC** (BTC/ETH). |
| [GITHUB_ROADMAP.md](GITHUB_ROADMAP.md) | **Milestones e issues** en GitHub: research cripto, Cortex, expansión política/deportes/clima/economía. |

---

## Resumen por estrategia

- **Monte Carlo Sniper** (estrategia en producción): nombre oficial, simulación GBM del precio, edge vs libro, una pierna (YES o NO) por señal. Ficha: [ESTRATEGIA_MONTECARLO_SNIPER.md](ESTRATEGIA_MONTECARLO_SNIPER.md). Viabilidad: [VIABILIDAD_ESTRATEGIA_MONTECARLO.md](VIABILIDAD_ESTRATEGIA_MONTECARLO.md).
- **Double-cheap straddle** (pendiente de wallet para producción): órdenes límite en ambas piernas cuando el ask baja de un umbral; evaluación con snapshots reales del CLOB. Documentación: [SIMULACION_DOUBLE_CHEAP_STRADDLE.md](SIMULACION_DOUBLE_CHEAP_STRADDLE.md).
