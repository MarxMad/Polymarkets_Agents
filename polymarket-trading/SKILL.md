---
name: polymarket
description: Analyze active markets, fetch real-time prices, and place bets on Polymarket (Polygon).
metadata: {"clawdbot":{"emoji":"📊"}}
---

# Polymarket Trading Engine v2

Expert skill for prediction market analysis and execution on Polymarket.

## Usage

### Listar mercados activos (los más relevantes)
```bash
export $(grep -v '^#' ~/.openclaw/.env | xargs) && ~/.venv/bin/python3 ~/.openclaw/workspace/skills/polymarket/scripts/trade.py list -n 10
```

### Buscar mercados por tema
```bash
export $(grep -v '^#' ~/.openclaw/.env | xargs) && ~/.venv/bin/python3 ~/.openclaw/workspace/skills/polymarket/scripts/trade.py list -q "bitcoin" -n 10
```

### Ver categorías disponibles
```bash
export $(grep -v '^#' ~/.openclaw/.env | xargs) && ~/.venv/bin/python3 ~/.openclaw/workspace/skills/polymarket/scripts/trade.py tags
```

### Verificar conexión API
```bash
export $(grep -v '^#' ~/.openclaw/.env | xargs) && ~/.venv/bin/python3 ~/.openclaw/workspace/skills/polymarket/scripts/trade.py status
```

### Apostar (requiere Private Key)
```bash
export $(grep -v '^#' ~/.openclaw/.env | xargs) && ~/.venv/bin/python3 ~/.openclaw/workspace/skills/polymarket/scripts/trade.py bet --token TOKEN_ID --amount 10 --price 0.5
```

## Credentials
The skill reads from `~/.openclaw/.env`. Required: POLYMARKET_API_KEY, POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE, POLYMARKET_PRIVATE_KEY.
