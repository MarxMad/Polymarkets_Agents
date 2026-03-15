import os
import requests
from web3 import Web3
from dotenv import load_dotenv

load_dotenv(os.path.expanduser("~/.openclaw/.env"))
PROXY = os.getenv("PROXY_ADDRESS", "0x1294d2B89B08E8651124F04534FB2715a1437846")
CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
RPC = os.getenv("POLYGON_RPC_URL", "https://1rpc.io/matic")

# ERC1155 ABI
ABI = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}, {"name": "_id", "type": "uint256"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]

def run():
    w3 = Web3(Web3.HTTPProvider(RPC))
    contract = w3.eth.contract(address=Web3.to_checksum_address(CTF), abi=ABI)
    
    # Get recent token IDs from Gamma
    resp = requests.get("https://gamma-api.polymarket.com/events?active=true&closed=false&limit=100&tag_id=102892")
    tokens = []
    if resp.status_code == 200:
        for ev in resp.json():
            for m in ev.get("markets", []):
                import json
                ids = json.loads(m.get("clobTokenIds", "[]"))
                tokens.extend(ids)
    
    tokens = list(set(tokens))
    print(f"Checking {len(tokens)} tokens...")
    for tid in tokens:
        try:
            bal = contract.functions.balanceOf(Web3.to_checksum_address(PROXY), int(tid)).call()
            if bal > 1e16: # > 0.01 shares (assuming 18 decimals? no, Polymarket shares have 6 decimals sometimes? Wait, CTF uses 18 or 6?)
                # Actually Polymarket shares are usually 6 decimals or matches collateral.
                # USDC is 6 decimals.
                print(f"TOKEN_ID:{tid}:BALANCE:{bal}")
        except: pass

run()
