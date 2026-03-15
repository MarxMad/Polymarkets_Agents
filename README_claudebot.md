# 🛰️ SWARM SNIPER v18: Roadmap to $50,000

A high-frequency algorithmic sniper designed for **Polymarket Binary Markets** (Predicting Crypto price movements in 5-minute windows). This bot utilizes a **Zero-Lag Swarm Intelligence** strategy, synchronizing real-time Binance data with Polymarket's order book.

---

## 🎯 The Strategy: "Terminal Sniper v18 (Strict)"

The mission is to turn $150 into $50,000 using precision and capital protection.

### 🛡️ 1. Risk/Reward Filter ($0.90 Rule)
*   **Max Entry Price**: `$0.90 USDC.e`
*   **Minimum ROI**: `11.11%` per trade.
*   **Logic**: We no longer accept "scraps". By capping entry at $0.90, we ensure that every winning trade pays out significantly more than the cost of gas and potential minor slips.

### 🧬 2. Directional Delta (Binance Sync)
*   **Activation Threshold**: `0.15%`
*   **Sync Source**: Real-time Binance spot prices.
*   **Logic**: The bot only enters a position if Binance confirms a strong trend. If BTC/ETH/SOL moves more than 0.15% away from the market's baseline in the last minute, the Sniper fires.

### ⏱️ 3. Execution Window (The 80s Zone)
*   **Sniper Window**: `10s - 85s` before market close.
*   **Precision**: Operating in the final seconds maximizes prediction accuracy while preserving enough liquidity to execute orders at a fair price.

---

## 🗺️ Roadmap to $50k

| Phase | Capital Range | Strategy | Risk Management |
| :--- | :--- | :--- | :--- |
| **Phase 1: Accumulation** | $150 → $1,000 | **Strict Sniper v18** | Fixed $1.00 Ticket Size |
| **Phase 2: Compounding** | $1,000 → $5,000 | Scaling Size | $10.00 Ticket Size (1% ROI Min) |
| **Phase 3: Hypergrowth** | $5,000 → $50,000 | Swarm Scaling | Dynamic Scaling (1% of Portfolio) |

---

## ⚙️ Technical Architecture

### 🏦 Smart Contract Wallet (Gnosis Safe)
The system uses the **Signer/Proxy Architecture** for maximum security:
*   **Signer (EOA)**: Holds only a small amount of POL (Gas). This is the only key the bot "sees".
*   **Proxy (Gnosis Safe)**: Holds the actual USDC.e capital. It requires a signature from the Signer but cannot be drained directly by the bot.

### 💰 Optimized Auto-Claim (GAS Saving)
*   **Interval**: Every 8 minutes.
*   **Targeting**: Only the last 10 unique markets.
*   **GAS Priority**: 50% extra priority to avoid stuck transactions.
*   **Gas Efficiency**: By limiting the scan, we ensure that 2 POL of gas lasts for weeks of continuous operation.

---

## 🔒 Security & Privacy

> [!IMPORTANT]
> **Key Protection**: All sensitive information is handled through environment variables (`.env`).
> - **POLYMARKET_PRIVATE_KEY**: Encrypted and never hardcoded.
> - **API Credentials**: Secured in the `.openclaw` infrastructure.
> - **RPC URLs**: Private endpoints used to prevent censorship/front-running.

**Never share your `.env` file or commit it to GitHub.**

---

## 📊 Performance Monitoring
The bot provides real-time logs and Telegram heartbeats:
- **Cash Balance Monitoring**: Automatic update every 30 seconds.
- **Estimated Profit Analysis**: Calculated before execution.
- **Terminal Execution Logs**: Detailed timestamps of every "Snipe".

---
## 📚 Documentación

El resto de la documentación del proyecto (guías, credenciales, despliegue, agent-docs) está en la carpeta **[docs/](docs/)**.

---
*Developed by the Swarm Intel Team for the $50k Challenge (v18).*
