import os
import requests
from web3 import Web3
from eth_account import Account
from dotenv import load_dotenv
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("EmergencyRedeem")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

# Config
PROXY_ADDRESS = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
RPC_URL = os.getenv("POLYGON_RPC_URL", "https://1rpc.io/matic")

# ABI for Redemption
CTF_ABI = [{"constant":False,"inputs":[{"name":"collateralToken","type":"address"},{"name":"parentCollectionId","type":"bytes32"},{"name":"conditionId","type":"bytes32"},{"name":"indexSets","type":"uint256[]"}],"name":"redeemPositions","outputs":[],"type":"function"}]

def get_positions():
    """Fetches current positions from Polymarket Gamma API for the proxy."""
    url = f"https://gamma-api.polymarket.com/positions?user={PROXY_ADDRESS}"
    resp = requests.get(url)
    if resp.status_code == 200:
        return resp.json()
    return []

def run():
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    if not pk: 
        log.error("No private key found")
        return
    
    account = Account.from_key(pk)
    log.info(f"Usando cuenta: {account.address}")
    
    positions = get_positions()
    log.info(f"Encontradas {len(positions)} posiciones en el perfil.")
    
    resolved_cids = []
    for pos in positions:
        # Check if the market is closed
        cid = pos.get("conditionId")
        if cid and cid not in resolved_cids:
            # Check market status
            m_resp = requests.get(f"https://gamma-api.polymarket.com/markets?condition_id={cid}")
            if m_resp.status_code == 200:
                m_data = m_resp.json()
                if m_data and m_data[0].get("closed"):
                    resolved_cids.append(cid)
    
    if not resolved_cids:
        log.info("No hay posiciones en mercados cerrados para redimir.")
        return

    log.info(f"🚀 Redimiendo {len(resolved_cids)} mercados resueltos...")
    
    # NOTE: Since the proxy is a Safe, a direct call from EOA to CTF won't work 
    # IF the tokens are inside the Proxy. 
    # The user should use the Polymarket UI "Redeem" button for now, 
    # as Safe signing is complex.
    
    for cid in resolved_cids:
        log.info(f"Mercado listo para redimir en UI: https://polymarket.com/event-condition/{cid}")

run()
