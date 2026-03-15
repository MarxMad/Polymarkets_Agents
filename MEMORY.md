# MEMORY — Contexto y decisiones

> Registro de contexto para que PumaClaw mantenga coherencia.

---

## Contexto actual

| Fecha | Contexto | Notas |
|-------|----------|-------|
| 2026-02-22 | Polymarket Trading Engine v2 desplegado. | Gamma API + CLOB API funcionando. |
| 2026-02-22 | Private key configurada en .env. | Wallet lista para operar. |
| 2026-02-22 | Modelo cambiado a gpt-4o-mini. | Por rate limits en gpt-4o. |
| 2026-02-22 | Elevated permissions desactivadas. | Bot ejecuta sin pedir aprobación. |

---

## Decisiones tomadas

| Área | Decisión | Razón |
|------|----------|-------|
| Foco | 100% Polymarket trading | Meta: $1M USD en 12 meses. |
| API | Gamma API para descubrimiento | CLOB get_markets() devuelve mercados viejos. |
| Ejecución | Bash directo, sin sandbox Docker | sandbox.mode = off para acceso completo. |
| Modelo | gpt-4o-mini | Balance costo/velocidad/rate limits. |

---

## Recordatorios

- Siempre cargar env con `export $(grep -v '^#' ~/.openclaw/.env | xargs)` ANTES de ejecutar trade.py.
- Operador tiene Telegram ID: `1608242541`.
- Credenciales en `~/.openclaw/.env` — NUNCA exponer.