import os
import sys
import json
import logging
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, OrderArgs, AssetType, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL

# Logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("liquidator")

def get_clob_client():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
    PROXY_ADDRESS = "0x1294d2B89B08E8651124F04534FB2715a1437846"
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase)
    return ClobClient("https://clob.polymarket.com", key=pk, chain_id=POLYGON, creds=creds, signature_type=2, funder=PROXY_ADDRESS)

def liquidate():
    client = get_clob_client()
    log.info("Starting Corrected Liquidation with proper Token IDs...")

    # 1. Cancel all open orders
    try:
        log.info("Cancelling all open orders...")
        client.cancel_all()
    except Exception as e:
        log.error(f"Error cancelling orders: {e}")

    # 2. Correct Token IDs from debug_account.py
    targets = [
        ("MegaETH No", "102844052859529992637803443259193395522411387362312885030298797134413940349829"),
        ("Trump No", "30442780799048074404860985387051749017905070253466005720364298335239299761065"),
        ("Kraken Yes", "114134832775772592035076635156705298512014414341622748270460538141767888144004")
    ]

    for name, tid in targets:
        try:
            p = BalanceAllowanceParams(asset_type="CONDITIONAL", token_id=tid, signature_type=2)
            b = client.get_balance_allowance(p)
            qty = float(b.get("balance", 0)) / 1e6
            
            if qty > 0.1:
                log.info(f"Liquidating {qty} shares of {name} ({tid[:8]}...)")
                book = client.get_order_book(tid)
                if book.bids:
                    best_bid = float(book.bids[0].price)
                    log.info(f"Selling {qty} shares at best bid {best_bid}")
                    
                    order_args = OrderArgs(
                        price=best_bid,
                        size=qty,
                        side=SELL,
                        token_id=tid,
                    )
                    signed_order = client.create_order(order_args)
                    resp = client.post_order(signed_order)
                    log.info(f"Order successful: {resp}")
                else:
                    log.warning(f"No bids for {name}")
        except Exception as e:
            log.error(f"Failed to liquidate {name}: {e}")

    log.info("Liquidation complete.")

if __name__ == "__main__":
    liquidate()
