import os
import sys
import logging
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, BalanceAllowanceParams, AssetType
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger("Liquidation")

load_dotenv(os.path.expanduser("~/.openclaw/.env"))

def get_clob_client():
    creds = ApiCreds(
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE")
    )
    return ClobClient(
        "https://clob.polymarket.com",
        key=os.getenv("POLYMARKET_PRIVATE_KEY"),
        chain_id=137,
        creds=creds,
        signature_type=2,
        funder=os.getenv("FUNDING_ADDRESS", "0x2F7Ac1F09941320Ff9ff88dCb09eF1A9b40Ff098")
    )

def main():
    log.info("Starting Total Liquidation...")
    client = get_clob_client()
    
    # Cancel all open orders
    log.info("Canceling all open orders...")
    try:
        resp = client.cancel_all()
        log.info(f"Cancel all response: {resp}")
    except Exception as e:
        log.error(f"Failed to cancel open orders: {e}")
        
    # Liquidate positions visible in UI
    tokens = [
        "103693433518125527001416636574099415821922498558487623412396163292963814003978", # MegaETH 1.5B Yes (example)
        "81064263614611322976599369791100684944137505919329565340390899607649071360756",
        "33799186820745984796925628555218896548353763534512103584425851114581900224385",
        "28557614648090529004584076028720900603196666949274543515794672175624115225556",
        "34554555827438551101000555305203609600029621153428996114009350892614396532498",
        "88902058027062214140177978007942040532071439710160833384602336149457247354303",
        "114134832775772592035076635156705298512014414341622748270460538141767888144004",
        "30442780799048074404860985387051749017905070253466005720364298335239299761065",
        "38746974225100760605049476871091018825176822620072388068043468416649080453478",
        "15941169207766483447414689399123153340714132932920187738030167528134553690538",
        "59361386662140736273790755583050126642583489495616165418126619457111736694733",
        "57301498276970257025109591078431189727442302532145853906375186182281603517458",
        "46094258080196889322046056727689179412841124675494563561748811364384667201787",
        "79271933354357980253136288605181231275403247125972443314258298490063254253319"
    ]
    
    for t in tokens:
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=t)
            bal = client.get_balance_allowance(params)
            val = float(bal.get('balance', 0)) / 1e6
            if val > 0:
                log.info(f"Found {val} shares of token {t[:8]}...")
                try:
                    book = client.get_order_book(t)
                    bids = book.bids
                    if not bids:
                        log.warning("No bids, placing 0.01 limit order to sell...")
                        price = 0.01
                    else:
                        price = float(bids[0].price)
                        log.info(f"Best bid: {price}")
                    
                    order = OrderArgs(
                        price=price,
                        size=val,
                        side="SELL",
                        token_id=t
                    )
                    signed_order = client.create_order(order)
                    resp = client.post_order(signed_order)
                    log.info(f"Sell order resp: {resp.get('success', False)} - {resp.get('errorMessage', '')}")
                except Exception as e:
                    log.error(f"Error selling token {t}: {e}")
        except Exception as e:
            pass

    log.info("Liquidation run complete.")

if __name__ == '__main__':
    main()
