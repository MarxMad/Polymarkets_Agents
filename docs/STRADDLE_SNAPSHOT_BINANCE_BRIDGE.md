# Puente Straddle: snapshots (CLOB) + Binance + surrogate para histórico largo

Este documento fija las **definiciones (Fase 0)** y enlaza el pipeline implementado en `polymarket-trading/scripts/straddle_snapshot_binance_bridge.py`.

## Objetivo

1. **Unir** cada línea de `orderbook_snapshots.jsonl` con el subyacente Binance (strike al inicio del mercado, spot en el instante del snapshot, volatilidad corta).
2. **Describir** empíricamente la relación spot ↔ `yes_ask` / `no_ask` / spread (Fase 2).
3. **Entrenar** un modelo surrogate (Random Forest) que prediga asks desde features de estado (Fase 3).
4. **Validar** con split temporal y métricas de error (Fase 5).
5. **Simular** ventanas sintéticas de 5 minutos sobre muchos días de velas 1m Binance, con **bandas p10–p50–p90** de PnL (Fase 4).
6. **Operación**: seguir acumulando JSONL real (`pumaclaw-orderbook-recorder`) para endurecer el modelo (Fase 6).

## Definiciones (Fase 0)

### Strike por mercado (`market_id`)

Igual que en el resto del repo:

1. `GET https://gamma-api.polymarket.com/markets/{market_id}` → `eventStartTime` o `startDate` (ISO UTC).
2. **Strike** = precio **open** de la vela **1m** de Binance (`BTCUSDT` / `ETHUSDT`) cuyo `startTime` coincide con el minuto de inicio del evento (misma convención que `sniper_param_search_on_snapshots.py`).

Si no hay strike válido, la fila se descarta en `join`.

### Spot en el instante del snapshot

**Spot** = `close` de la vela 1m de Binance con `openTime = floor(ts_snapshot_utc / 60s) * 1000` (precio al cierre de ese minuto, alineado al minuto del snapshot).

### Features (vector de estado)

| Feature | Descripción |
|--------|-------------|
| `log_m` | `log(spot / strike)` |
| `tl_norm` | `time_left_s / 300` (clamped a [0, 2]) |
| `tl_norm2` | `tl_norm ** 2` |
| `vol_15m` | Desviación estándar de retornos log de los últimos **15** cierres 1m **estrictamente anteriores** al minuto del snapshot; escalado `* sqrt(525600)` y clamp [0.05, 3.0] (análogo de escala a otros scripts). |
| `is_btc` | 1 si ticker BTC, 0 si ETH |
| `hour_sin`, `hour_cos` | Codificación cíclica de la hora UTC del snapshot |
| `dow_sin`, `dow_cos` | Día de la semana UTC (0=lunes) |

### Objetivos del surrogate

- `yes_ask`, `no_ask` (en (0,1)) como regresión separada (dos Random Forests).
- Incertidumbre: percentiles **p10, p50, p90** por predicción usando las predicciones de cada árbol.

### Eventos “pierna barata” (análisis straddle)

- **Touch YES**: `yes_ask <= limit_price` (por defecto 0.35 en descriptivos).
- **Touch NO**: análogo.
- Se reporta distribución de `log_m`, `time_left_s` y **otra pierna** en el instante del touch.

### Simulación sintética 5m (Fase 4)

- Ventanas de **300 s** avanzando en pasos de **60 s** sobre la serie 1m (solapadas).
- En **t = 0, 60, 120, 180, 240, 300 s** desde el inicio de la ventana: spot = close 1m, features → predicción surrogate de asks.
- Entre nodos: **interpolación lineal** del ask predicho para muestrear cada **5 s** (61 puntos) y aplicar la **opción 2** (misma semántica que `straddle_optimizer.simulate_option2`: primer cheap, confirmación de la otra pierna dentro de `confirm_sec`, timeout, salida al bid en stop). **Fees desactivados** en la ruta sintética (tokens desconocidos).

### Gates de confianza (Fase 5)

- Split temporal por **timestamp de fila** (no aleatorio): por defecto **70% train / 30% test** ordenado por `ts`.
- Reportar MAE / RMSE / R² para `yes_ask` y `no_ask` en test.
- Si el error es alto o inestable entre mitades del periodo, **no** interpretar la simulación de 3 meses como PnL real; solo como **rango exploratorio**.

## Comandos (resumen)

En macOS/Linux con PEP 668, conviene un venv:

```bash
cd polymarket-trading
python3 -m venv .venv-bridge
.venv-bridge/bin/pip install -r requirements-bridge.txt
```

Luego usa `.venv-bridge/bin/python3` (o activa el venv) para los comandos siguientes.

```bash
# 1) Join (genera filas + meta de mercados; usar stride en pruebas rápidas)
python3 polymarket-trading/scripts/straddle_snapshot_binance_bridge.py join \
  --snapshots ~/orderbook_snapshots.jsonl --out-dir ./bridge_out --stride 3

# 2) Descriptivo
python3 polymarket-trading/scripts/straddle_snapshot_binance_bridge.py describe \
  --features ./bridge_out/features.jsonl.gz --out ./bridge_out/descriptive.json

# 3) Entrenar surrogate
python3 polymarket-trading/scripts/straddle_snapshot_binance_bridge.py train \
  --features ./bridge_out/features.jsonl.gz --out-dir ./bridge_out

# 4) Validar (holdout temporal)
python3 polymarket-trading/scripts/straddle_snapshot_binance_bridge.py validate \
  --features ./bridge_out/features.jsonl.gz --model-dir ./bridge_out

# 5) Simular 90 días Binance (bandas)
python3 polymarket-trading/scripts/straddle_snapshot_binance_bridge.py simulate-binance \
  --model-dir ./bridge_out --days 90 --out ./bridge_out/sim_binance_90d.json
```

## Fase 6 — Datos reales

- Mantener activo `pumaclaw-orderbook-recorder.service` para **ampliar** `orderbook_snapshots.jsonl`.
- Re-ejecutar `join` → `train` cuando haya **≥4–8 semanas** nuevas; comparar coeficientes de error y estabilidad del ranking de PnL sintético.
