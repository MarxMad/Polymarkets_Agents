# 🎯 PumaClaw: Monte Carlo Sniper v1.0
> **Mission:** Escalar de $50 USD a $1,000,000 USD mediante ventaja matemática estricta y reducción sistemática del "Risk of Ruin".

![Polymarket Banner](https://polymarket.com/_next/image?url=%2Fimages%2Fhomepage%2Fhero-bg.webp&w=3840&q=75)

El **Monte Carlo Sniper** es la evolución definitiva en arquitectura de agentes autónomos para Polymarket. En lugar de procesar "vibes" o intentar adivinar sentimientos basados en modelos de lenguaje (LLM), este sistema opera exclusivamente en **Probabilidad Bayesiana** y **Modelado Estocástico**.

Si la masa opina que algo tiene un 18% de probabilidad, pero la simulación calcula un 73%, no es un trade, es **dinero asegurado**.

---

## ⚙️ Arquitectura Cuantitativa

El agente abandona las predicciones lentas y adopta herramientas matemáticas de nivel prop-firm en Wall Street para operar los mercados binarios de 5 y 15 minutos en Cripto (BTC & ETH).

### 1. Extracción de Volatilidad en Vivo (Binance)
En lugar de mirar el `priceToBeat` estático, el agente se conecta al *websocket/API* de Binance para calcular la **Volatilidad Histórica (HV)** y el **Drift** del activo utilizando retornos logarítmicos de las últimas 60 velas de un minuto.

### 2. Motor de Simulación (GBM)
Usando el *Geometric Brownian Motion* (Movimiento Browniano Geométrico), el agente proyecta la trayectoria del precio actual hacia el futuro **10,000 veces distintas** en milisegundos.

### 3. Edge Detection (Detección de Ventaja)
De los 10,000 futuros posibles simulados, el motor cuenta cuántas veces el activo cruza el *Strike Price* del mercado. El resultado es la **Probabilidad Verdadera**. 

*Ejemplo:*
- **Simulación (Math):** 62% de probabilidad de YES.
- **Mercado (Polymarket):** Option YES vendiéndose a 31¢ (31%).
- **Resultado:** *Massive mispricing*. El agente detecta un Edge de +31%.

### 4. Ejecución Protectora (Fractional Kelly)
Para proteger el limitado **capital de $50 USD** y garantizar el camino hacia $1M, no se hacen apuestas planas de $10. El agente usa el **Criterio de Kelly** para calcular el tamaño matemáticamente perfecto de la posición para maximizar ganancias sin quebrar, acortado con una fracción (Safe Kelly) de mitigación del riesgo.

---

## 📊 Dashboard Visualizador

El sistema incluye una consola táctica basada en **Dash/Plotly** que permite visualizar en tiempo real cómo el agente computa el caos del mercado.

![Dashboard Preview](https://github.com/user-attachments/assets/placeholder-image-montecarlo-dashboard.png)

### ¿Qué te muestra?
- **Gráfica Estocástica:** La renderización interactiva de 150 a 10,000 proyecciones de precio (Verde = WIN, Rojo = LOSS).
- **Target Line (Strike Price):** La línea divisoria donde se define la resolución del mercado.
- **Scoreboard Comparativo:** 
  - `Math Derived <YES>` (Lo que dice la estadística) 
  - `Market Price <YES>` (El pánico o euforia de humanos en Polymarket)
  - `Current Edge` (Tu ventana de oportunidad)

---

## 🚀 Despliegue Seguro e Infranqueable (Seguridad Local)

**Tu entorno local en Mac se mantiene 100% blindado y aislado.** Todo el procesamiento masivo y la ejecución de órdenes ocurren directamente en la instancia de AWS (EC2).

### 1. Iniciar la Nube (AWS EC2)

1. Conecta con Puerto Seguro usando un **Túnel SSH** para traer los gráficos a salvo a tu Mac:
```bash
ssh -i ~/Documents/AWS/PassPuma.pem -L 8050:localhost:8050 ubuntu@ec2-3-255-209-58.eu-west-1.compute.amazonaws.com
```

2. Dentro del servidor, enciende el visualizador:
```bash
cd ~/.openclaw/workspace/skills/polymarket/
~/.venv/bin/pip install numpy dash dash-bootstrap-components pandas plotly
~/.venv/bin/python3 scripts/montecarlo_viz.py
```

### 2. Ver en tu Mac sin Riesgo

Tu Mac no ejecuta dependencias raras ni OpenClaw toma control. Solo necesitas abrir tu navegador (Chrome o Safari) y mirar desde la barrera cómo tu instancia en Amazon trabaja por ti en un puerto reflejado seguro.

👉 `http://localhost:8050`

---

## 📈 Siguientes Pasos (Road a $1M)

1. **Fase de Observación (Ghost Mode):**
   Usa el visualizador durante un día completo para certificar que el Edge detectado es consecuente y el spread de Polymarket es superado.

2. **Fase Acumuladora (Sniper Activado):**
   Activa el código de ejecución en `montecarlo_sniper.py`. Deja que el agente invierta mini-lotes ($2 - $4) con Kelly Fraccional y acumule ganancias de centavos.

3. **Efecto de Interés Compuesto:**
   Al seguir las leyes del Criterio de Kelly, conforme tus $50 USD se conviertan en $250 USD, el bot automáticamente aumentará el tamaño de sus disparos. Cuando llegues a $2,500 USD, las entradas serán del triple tamaño, siempre con seguro matemático contra riesgo de ruina.

*“Most traders bet on vibes. We bet on 10,000 simulated futures.”*
