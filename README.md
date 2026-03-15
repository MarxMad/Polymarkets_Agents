# Polimarkets_Bots

Código base de los bots de Polymarket (PumaClaw/claudebot), copiado para desarrollar un **nuevo agente con OpenClaw** con meta: **$1M USD**.

**Trabajo en la instancia:** el desarrollo y la optimización se hacen en la EC2, no en local. Este repo/carpeta sirve de referencia y para sincronizar código hacia la instancia.

## Entrar a la instancia

Conexión SSH a la EC2 (región eu-west-1):

```bash
ssh -i ~/Documents/AWS/PassPuma.pem ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com
```

- **Usuario:** `ubuntu`
- **Clave:** `~/Documents/AWS/PassPuma.pem`
- **Host:** `ec2-3-255-209-58.eu-west-1.compute.amazonaws.com`

En la instancia, el workspace de OpenClaw suele estar en `~/.openclaw/workspace/` y los skills de Polymarket en `~/.openclaw/workspace/skills/polymarket/`. Credenciales en `~/.openclaw/.env`.

## Estructura

```
Polimarkets_Bots/
├── README.md                 ← Este archivo (objetivo 1M, nuevo agente)
├── SCRIPTS_README.md         ← Scripts y credenciales
├── DEPLOY_SCALPER.md         ← Despliegue scalper
├── GUIA_OPENCLAW_INICIO.md   ← Guía OpenClaw
├── MEMORY.md                 ← Memoria del agente anterior
├── README_V14_QUANTUM.md     ← Visión cuantitativa / Tag-Less Radar
├── README_claudebot.md       ← README original del repo claudebot
└── polymarket-trading/       ← Skill completo: scripts, services, SKILL.md
    ├── scripts/              ← trader, trade, listener, hedger, scalper, redeem, etc.
    ├── pumaclaw-*.service     ← systemd (trader, listener, hedger, blind, report)
    ├── pumaclaw-report.timer
    ├── SKILL.md
    └── strategy.json
```

## Objetivo

- **Meta:** $1,000,000 USD operando en Polymarket con OpenClaw.
- **Enfoque:** Nuevo agente OpenClaw que use este código como skill (Gamma API + CLOB), con estrategias, risk management y automatización mejorados.

## Próximos pasos

1. Definir arquitectura del nuevo agente (OpenClaw + skills/tools).
2. Revisar y refactorizar scripts clave (`trader.py`, `trade.py`, hedger, scalper).
3. Configurar OpenClaw (openclaw.json, tools, memoria) para este workspace.
4. Backtesting / paper trading antes de capital real.
5. Desplegar en instancia (EC2 o similar) y monitoreo.

### Sincronizar código hacia la instancia

Desde tu máquina (en la carpeta `Polimarkets_Bots` o `polymarket-trading`):

```bash
rsync -avz -e "ssh -i ~/Documents/AWS/PassPuma.pem" \
  polymarket-trading/ \
  ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com:~/.openclaw/workspace/skills/polymarket/
```

No sobrescribir en el servidor `config.json` ni `~/.openclaw/.env` (contienen secretos).

## Credenciales (en la instancia)

No incluidas en este repo. En la instancia se usan desde `~/.openclaw/.env`:

- `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE`
- `POLYMARKET_PRIVATE_KEY`, `PROXY_ADDRESS`
- Opcional: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`

Ver `SCRIPTS_README.md` y `polymarket-trading/SKILL.md`.
