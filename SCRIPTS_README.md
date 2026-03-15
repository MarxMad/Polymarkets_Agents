# Scripts y skills en este repo

Scripts traídos desde la instancia OpenClaw/PumaClaw. **No se incluyen** private keys, API keys ni `config.json` con secretos.

## Estructura

- **skills/polymarket-trading/** — Polymarket: hedger, scalper, listener, trader, trade, liquidate, redeem, etc.
- **skills/twitter-api/** — Tweet desde el agente (OAuth 1.0a).

## Credenciales (solo en instancia, nunca en git)

- **Polymarket:** se leen de `~/.openclaw/.env`: `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`, `POLYMARKET_PRIVATE_KEY`, `PROXY_ADDRESS`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`.
- **Twitter:** copiar `config.example.json` a `config.json` en la skill y rellenar con las claves de developer.x.com (Read and Write).

## .gitignore

- `skills/**/config.json` — no se suben configs con keys.
- `.env`, `*.pem`, `*credentials*`, `scalper_trades.csv` — excluidos.

## Desplegar a la instancia

Usar `rsync` o `scp` hacia `~/.openclaw/workspace/skills/` (polymarket o twitter-api). No sobrescribir `config.json` ni `.env` en el servidor.
