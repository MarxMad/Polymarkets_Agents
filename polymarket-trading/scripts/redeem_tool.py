import os
import time
import json
import logging
import requests
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds
from py_clob_client.constants import POLYGON

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("Redeemer")

# Load Env
load_dotenv(os.path.expanduser("~/.openclaw/.env"))

# Contracts
CTF_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDC_E_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PROXY_ADDRESS = "0x1294d2B89B08E8651124F04534FB2715a1437846"

# Minimal ABIs
CTF_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"}
        ],
        "name": "redeemPositions",
        "outputs": [],
        "type": "function"
    }
]

SAFE_ABI = [
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "value", "type": "uint256"},
            {"name": "data", "type": "bytes"},
            {"name": "operation", "type": "uint8"},
            {"name": "safeTxGas", "type": "uint256"},
            {"name": "baseGas", "type": "uint256"},
            {"name": "gasPrice", "type": "uint256"},
            {"name": "gasToken", "type": "address"},
            {"name": "refundReceiver", "type": "address"},
            {"name": "signatures", "type": "bytes"}
        ],
        "name": "execTransaction",
        "outputs": [{"name": "success", "type": "bool"}],
        "type": "function"
    },
    {
        "inputs": [],
        "name": "nonce",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function"
    }
]

def get_unique_condition_ids():
    log.info("Buscando ConditionIds en el historial de trades...")
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
    
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=137, creds=creds)
    
    try:
        trades = client.get_trades()
        # El campo 'market' en los trades de CLOB suele coincidir con el conditionId
        c_ids = list(set([t.get('market') for t in trades if t.get('market')]))
        
        # Validar cuales están cerrados en Gamma
        valid_c_ids = []
        for cid in c_ids[:40]: # Checar los últimos 40 mercados
            r = requests.get(f"https://gamma-api.polymarket.com/markets?condition_id={cid}")
            if r.status_code == 200:
                data = r.json()
                if data and data[0].get('closed'):
                    valid_c_ids.append(cid)
        
        log.info(f"Encontrados {len(valid_c_ids)} ConditionIds cerrados para redimir.")
        return valid_c_ids
    except Exception as e:
        log.error(f"Error obteniendo ConditionIds: {e}")
        return []

def redeem_positions(condition_ids):
    if not condition_ids:
        return
    
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://1rpc.io/matic")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    account = Account.from_key(pk)
    
    safe_contract = w3.eth.contract(address=Web3.to_checksum_address(PROXY_ADDRESS), abi=SAFE_ABI)
    ctf_contract = w3.eth.contract(address=Web3.to_checksum_address(CTF_ADDRESS), abi=CTF_ABI)
    
    for cid in condition_ids:
        try:
            log.info(f"⏳ Intentando redimir ConditionId: {cid}")
            
            # Encode redeemPositions call
            redeem_data = ctf_contract.encode_abi("redeemPositions", [
                Web3.to_checksum_address(USDC_E_ADDRESS),
                "0x" + "0" * 64,
                cid,
                [1, 2] # Outcomes 0 y 1
            ])
            
            nonce = safe_contract.functions.nonce().call()
            
            # EIP-712 Safe Transaction Hash
            # Simplified: Many 1-owner safes allow a simple signature
            # We construct the hash exactly as the Safe expects
            
            # For simplicity, we'll try to just send it if the account has POL
            # But the interaction must be signed.
            
            # Implementation of Safe Transaction signing is non-trivial without safe-eth-py
            # Let's see if we can just use a simpler method or if the account can call directly.
            
            log.warning("Redención via Gnosis Safe requiere firma EIP-712 compleja. Probando ejecución mínima...")
            
            # TODO: Add full signature logic if needed. 
            # For now, let's report what we found.
            
        except Exception as e:
            log.error(f"Fallo en {cid}: {e}")

if __name__ == "__main__":
    c_ids = get_unique_condition_ids()
    # redeem_positions(c_ids) # Pendiente de firma
    log.info("Script de redención preparado. Identificando mercados listos...")
    for cid in c_ids:
        log.info(f" - Mercado listo para Liquidar: {cid}")
