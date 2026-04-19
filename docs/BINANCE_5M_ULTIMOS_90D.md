# Velas Binance 5m — últimos 90 días (datos y resumen)

Datos descargados desde la API pública `GET /api/v3/klines` (intervalo `5m`).

**Análisis extendido (rachas consecutivas verdes/rojas y volatilidad por hora UTC):** ver [`BINANCE_5M_90D_RACHAS_Y_VOL_HORA.md`](BINANCE_5M_90D_RACHAS_Y_VOL_HORA.md). Se genera con `scripts/analyze_binance_5m_csv.py` sobre estos CSV.

## Archivos de datos (CSV)

- `data/binance/btcusdt_5m_90d.csv` (ruta relativa a la carpeta `polymarket-trading/`)
- `data/binance/ethusdt_5m_90d.csv` (ruta relativa a la carpeta `polymarket-trading/`)

Cada fila replica los campos de `GET /api/v3/klines` más la columna `symbol`.

| Columna CSV | Campo Binance |
|---------------|----------------|
| `symbol` | Par (añadido por el script) |
| `open_time_ms` | Inicio de la vela (ms UTC) |
| `open`, `high`, `low`, `close` | OHLC |
| `volume` | Volumen en activo base |
| `close_time_ms` | Fin de la vela (ms UTC) |
| `quote_asset_volume` | Volumen en activo cotización |
| `n_trades` | Número de trades |
| `taker_buy_base_volume` | Volumen compra taker (base) |
| `taker_buy_quote_volume` | Volumen compra taker (quote) |
| `ignore` | Campo ignorado (API) |

## Resumen estadístico

### BTCUSDT

| Campo | Valor |
|--------|-------|
| Primer `open_time` (UTC) | 2026-01-19T03:40:00+00:00 |
| Último `open_time` (UTC) | 2026-04-19T03:35:00+00:00 |
| Velas (`n_candles`) | 25920 |
| Cierre **>** apertura (n) | 12933 (49.90 %) |
| Cierre **<** apertura (n) | 12957 (49.99 %) |
| Cierre **=** apertura (n) | 30 (0.1157 %) |
| Base rango (H−L) % | `open` (respecto al open de la vela salvo `mid`) |
| Rango (H−L) % — media | 0.208476 |
| Rango (H−L) % — mediana | 0.160389 |
| Rango (H−L) % — p10 / p90 | 0.061612 / 0.403140 |
| Rango (H−L) % — std | 0.178136 |
| Cuerpo abs. (C−O) % del open — media | 0.110077 |
| Cuerpo abs. (C−O) % del open — mediana | 0.070533 |
| Sesgo (C−O) % del open — media | -0.000639 |
| Sesgo (C−O) % del open — mediana | 0.000000 |

### ETHUSDT

| Campo | Valor |
|--------|-------|
| Primer `open_time` (UTC) | 2026-01-19T03:40:00+00:00 |
| Último `open_time` (UTC) | 2026-04-19T03:35:00+00:00 |
| Velas (`n_candles`) | 25920 |
| Cierre **>** apertura (n) | 12946 (49.95 %) |
| Cierre **<** apertura (n) | 12918 (49.84 %) |
| Cierre **=** apertura (n) | 56 (0.2160 %) |
| Base rango (H−L) % | `open` (respecto al open de la vela salvo `mid`) |
| Rango (H−L) % — media | 0.278776 |
| Rango (H−L) % — mediana | 0.207904 |
| Rango (H−L) % — p10 / p90 | 0.084053 / 0.545232 |
| Rango (H−L) % — std | 0.252010 |
| Cuerpo abs. (C−O) % del open — media | 0.142114 |
| Cuerpo abs. (C−O) % del open — mediana | 0.087933 |
| Sesgo (C−O) % del open — media | -0.000948 |
| Sesgo (C−O) % del open — mediana | 0.000000 |

## Interpretación breve

- Si **% cierre > apertura** y **% cierre < apertura** son similares (~50 %), en 5m no hay sesgo direccional fuerte en la muestra.
- **Rango (H−L) % del open**: mide cuánto se mueve el precio *dentro* de la vela; comparar BTC vs ETH sirve para calibrar stops o umbrales de “movimiento típico” en 5m.
- **Sesgo (C−O) % del open** cercano a 0: la suma de retornos de cuerpo en 5m es casi neutra en el periodo (no implica ausencia de tendencias en escalas mayores).

## Notas

- Los porcentajes de rango y cuerpo usan el **open** de cada vela como denominador (salvo que en el script se use `--range-pct-basis mid`).
- Esto **no** sustituye datos de Polymarket (CLOB, shares); sirve como referencia de volatilidad y sesgo direccional en ventanas de 5 minutos.
- Para regenerar CSV y este resumen:

```bash
cd polymarket-trading
python3 scripts/binance_5m_candle_stats.py --days 90 --symbols BTCUSDT,ETHUSDT \
  --export-dir data/binance --stats-md ../docs/BINANCE_5M_ULTIMOS_90D.md
```

*Generado: 2026-04-19 03:35 UTC*
