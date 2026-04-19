# Roadmap en GitHub (milestones e issues)

Proyecto: **[MarxMad/Polymarkets_Agents](https://github.com/MarxMad/Polymarkets_Agents)**

## Milestones

| Milestone | Descripción |
|-----------|-------------|
| [Research y datos (cripto 5m)](https://github.com/MarxMad/Polymarkets_Agents/milestone/1) | Orderbook JSONL, Binance 5m, walk-forward y optimización de backtests. |
| [Cortex, laboratorios y documentación](https://github.com/MarxMad/Polymarkets_Agents/milestone/2) | Dashboard `montecarlo_cortex.py`, seguridad, tests. |
| [Expansión: política, deportes, clima, economía](https://github.com/MarxMad/Polymarkets_Agents/milestone/3) | Descubrimiento Gamma por vertical, recorder genérico, riesgo por categoría. |

## Issues creados (resumen)

**Milestone 1 — Research y datos**

- [#1](https://github.com/MarxMad/Polymarkets_Agents/issues/1) — CI: regenerar CSV Binance 5m y análisis rachas/vol horaria  
- [#2](https://github.com/MarxMad/Polymarkets_Agents/issues/2) — Infra: orderbook_recorder 24/7 y salud del JSONL  
- [#3](https://github.com/MarxMad/Polymarkets_Agents/issues/3) — Backtest: walk-forward temporal para straddle y sniper  
- [#4](https://github.com/MarxMad/Polymarkets_Agents/issues/4) — Optimizar runtime `simulate-binance` en straddle snapshot bridge  

**Milestone 2 — Cortex**

- [#5](https://github.com/MarxMad/Polymarkets_Agents/issues/5) — Docs: ficha completa del dashboard Cortex (8050)  
- [#6](https://github.com/MarxMad/Polymarkets_Agents/issues/6) — Seguridad: auth opcional para Dash expuesto por túnel  
- [#7](https://github.com/MarxMad/Polymarkets_Agents/issues/7) — QA: smoke tests para `montecarlo_cortex.py`  

**Milestone 3 — Expansión vertical**

- [#8](https://github.com/MarxMad/Polymarkets_Agents/issues/8) — Spike Gamma: política + resolución  
- [#9](https://github.com/MarxMad/Polymarkets_Agents/issues/9) — Spike Gamma: deportes  
- [#10](https://github.com/MarxMad/Polymarkets_Agents/issues/10) — Spike: mercados clima  
- [#11](https://github.com/MarxMad/Polymarkets_Agents/issues/11) — Spike: economía / macro  
- [#12](https://github.com/MarxMad/Polymarkets_Agents/issues/12) — Recorder orderbook por `tag_id` / vertical  
- [#13](https://github.com/MarxMad/Polymarkets_Agents/issues/13) — Riesgo: wallets y límites por vertical  

Para listar en CLI: `gh issue list -R MarxMad/Polymarkets_Agents`
