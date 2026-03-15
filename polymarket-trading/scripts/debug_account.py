import os
import json
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams

def main():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
    
    PROXY_ADDRESS = "0x1294d2B89B08E8651124F04534FB2715a1437846"
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    client = ClobClient("https://clob.polymarket.com", key=pk, chain_id=POLYGON, creds=creds, signature_type=2, funder=PROXY_ADDRESS)
    
    print("--- OPEN ORDERS ---")
    orders = client.get_orders()
    if not orders:
        print("No open orders.")
    for o in orders:
        print(f"ID: {o['id']} | {o['side']} {o['outcome']} | Size: {o['original_size']} | Matched: {o['size_matched']} | Price: {o['price']} | Market: {o['market']}")
    
    print("\n--- POSITIONS ---")
    tokens = [
        ("MegaETH No", "102844052859529992637803443259193395522411387362312885030298797134413940349829"),
        ("Trump No", "30442780799048074404860985387051749017905070253466005720364298335239299761065"),
        ("Kraken Yes", "114134832775772592035076635156705298512014414341622748270460538141767888144004"),
    ]
    for name, tid in tokens:
        params = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=tid, signature_type=2)
        bal = client.get_balance_allowance(params)
        print(f"{name}: {bal}")

if __name__ == "__main__":
    main()
