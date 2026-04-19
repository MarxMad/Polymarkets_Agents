# Recuento de operaciones: Monte Carlo Sniper

Datos extraídos de `~/trades_history.json` en la instancia. Solo se cuentan operaciones **resueltas** con campo `pnl`.

**Filtro aplicado:** se incluyen únicamente trades con **shares compradas en (0, 2]** para excluir operaciones hechas a mano (el bot usa máximo 1 USD y 2 shares por operación). Quedan excluidas **118** operaciones manuales.

**Rango de fechas (UTC):** 2026-03-16 03:13 — 2026-03-18 05:34

---

## 1. Resumen global (solo bot: shares ≤ 2)

| Métrica | Valor |
|---------|--------|
| **Total operaciones** | 90 |
| **Ganadoras** | 61 |
| **Perdedoras** | 29 |
| **Break-even** | 0 |
| **Win rate** | **67,8 %** |
| **PnL total (USD)** | **+15,23** |
| **PnL promedio por operación (USD)** | +0,17 |
| **Mayor drawdown (USD)** | −4,54 |

---

## 2. Por día (UTC)

| Fecha       | Operaciones | Ganadoras | Perdedoras | Win rate | PnL (USD) |
|-------------|-------------|-----------|------------|----------|-----------|
| 2026-03-16  | 17          | 12        | 5          | 70,6 %   | +6,41     |
| 2026-03-17  | 46          | 30        | 16         | 65,2 %   | +5,46     |
| 2026-03-18  | 27          | 19        | 8          | 70,4 %   | +3,36     |
| **Total**   | **90**      | **61**    | **29**     | **67,8 %** | **+15,23** |

*(El 2026-03-15 no aparece: todas las operaciones de ese día tenían > 2 shares y se consideran manuales.)*

---

## 3. Por activo (total)

| Activo | Operaciones | PnL (USD) |
|--------|-------------|-----------|
| BTC    | 48          | +9,18     |
| ETH    | 42          | +6,05     |
| **Total** | **90**    | **+15,23** |

---

## 4. Por activo y día (UTC)

### BTC

| Fecha       | Operaciones | PnL (USD) |
|-------------|-------------|-----------|
| 2026-03-16  | 10          | +6,21     |
| 2026-03-17  | 24          | +2,12     |
| 2026-03-18  | 14          | +0,85     |
| **Total**   | **48**      | **+9,18** |

### ETH

| Fecha       | Operaciones | PnL (USD) |
|-------------|-------------|-----------|
| 2026-03-16  | 7           | +0,20     |
| 2026-03-17  | 22          | +3,34     |
| 2026-03-18  | 13          | +2,51     |
| **Total**   | **42**      | **+6,05** |

---

## 5. Cómo actualizar este recuento

En la instancia (solo operaciones del bot, shares ≤ 2):

```bash
python3 polymarket-trading/scripts/recuento_operaciones_sniper.py
```

El script usa por defecto `SNIPER_MAX_SHARES_FILTER=2`. Para incluir todos los trades sin filtrar por tamaño:

```bash
SNIPER_MAX_SHARES_FILTER=999 python3 polymarket-trading/scripts/recuento_operaciones_sniper.py
```
