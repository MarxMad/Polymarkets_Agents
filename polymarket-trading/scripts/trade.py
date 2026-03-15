#!/usr/bin/env python3
"""
PumaClaw Polymarket Trading Engine v2
=====================================
Production-grade tool for market analysis and order execution on Polymarket.

Uses two APIs:
- Gamma API (gamma-api.polymarket.com) → market discovery, events, filtering
- CLOB API  (clob.polymarket.com)      → orderbook, prices, order execution
"""
import argparse
import json
import os
import sys
import requests

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, OrderArgs
from py_clob_client.order_builder.constants import BUY

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"


# ── Helpers ──────────────────────────────────────────────────────────────

def _is_valid_hex_key(key):
    """Check if a string looks like a valid hex private key."""
    if not key:
        return False
    clean = key.lstrip("0x")
    try:
        int(clean, 16)
        return len(clean) >= 32
    except ValueError:
        return False


def get_clob_client(authenticated=True):
    """Create a ClobClient for CLOB operations (orders, prices)."""
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")

    pk_valid = _is_valid_hex_key(pk)

    if authenticated and not pk_valid:
        raise Exception(
            "Missing or invalid POLYMARKET_PRIVATE_KEY. "
            "Set a valid hex private key in ~/.openclaw/.env"
        )

    effective_pk = pk if pk_valid else "0" * 64

    creds = ApiCreds(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase,
    ) if api_key else None

    return ClobClient(
        CLOB_HOST,
        key=effective_pk,
        chain_id=POLYGON,
        creds=creds,
        signature_type=0,
    )


# ── Gamma API (Market Discovery) ────────────────────────────────────────

def fetch_active_events(query=None, tag=None, limit=20, order="volume_24hr"):
    """
    Fetch active, open events from the Gamma API.
    
    Args:
        query: Text search filter
        tag: Category tag (e.g. 'Politics', 'Crypto', 'Sports')
        limit: Max results (default 20)
        order: Sort by 'volume_24hr', 'volume', 'liquidity', 'start_date', 'end_date'
    
    Returns:
        List of event dicts with markets nested inside.
    """
    params = {
        "active": "true",
        "closed": "false",
        "limit": min(limit, 100),
        "offset": 0,
    }
    if tag:
        params["tag"] = tag

    resp = requests.get(f"{GAMMA_API}/events", params=params, timeout=15)
    resp.raise_for_status()
    events = resp.json()

    # Filter by query text if provided
    if query:
        q = query.lower()
        events = [e for e in events if q in json.dumps(e).lower()]

    return events


def fetch_active_markets(query=None, tag=None, limit=20, order="volume_24hr"):
    """
    Fetch active markets (flattened from events) with clean summary data.
    Returns a simplified list suitable for the agent to present.
    """
    events = fetch_active_events(query=query, tag=tag, limit=limit, order=order)
    results = []

    for event in events:
        title = event.get("title", "Unknown")
        slug = event.get("slug", "")
        end_date = event.get("endDate", "N/A")
        volume = event.get("volume", 0)
        liquidity = event.get("liquidity", 0)
        
        markets = event.get("markets", [])
        for mkt in markets:
            if not mkt.get("active", False) or mkt.get("closed", False):
                continue

            outcomes = []
            tokens = mkt.get("outcomePrices", "[]")
            if isinstance(tokens, str):
                try:
                    prices = json.loads(tokens)
                except:
                    prices = []
            else:
                prices = tokens

            outcome_names = ["Yes", "No"]
            clobTokenIds = mkt.get("clobTokenIds", "[]")
            if isinstance(clobTokenIds, str):
                try:
                    token_ids = json.loads(clobTokenIds)
                except:
                    token_ids = []
            else:
                token_ids = clobTokenIds

            for i, price in enumerate(prices):
                outcomes.append({
                    "label": outcome_names[i] if i < len(outcome_names) else f"Outcome {i}",
                    "price": round(float(price), 3) if price else 0,
                    "token_id": token_ids[i] if i < len(token_ids) else None,
                })

            results.append({
                "event": title,
                "question": mkt.get("question", title),
                "slug": slug,
                "end_date": end_date,
                "volume_usd": round(float(volume), 2) if volume else 0,
                "liquidity_usd": round(float(liquidity), 2) if liquidity else 0,
                "outcomes": outcomes,
                "condition_id": mkt.get("conditionId", ""),
                "url": f"https://polymarket.com/event/{slug}" if slug else "",
                "accepting_orders": mkt.get("acceptingOrders", False),
            })

    return results[:limit]


def fetch_tags():
    """Fetch all available tags from the Gamma API."""
    resp = requests.get(f"{GAMMA_API}/tags", timeout=10)
    resp.raise_for_status()
    return resp.json()


# ── CLOB API (Trading) ──────────────────────────────────────────────────

def get_price(token_id):
    """Get current price for a specific token."""
    client = get_clob_client(authenticated=False)
    price = client.get_price(token_id, "BUY")
    return price


def place_bet(token_id, amount, price):
    """Place a BUY order on the specified outcome token."""
    client = get_clob_client(authenticated=True)
    order_args = OrderArgs(
        price=float(price),
        size=float(amount) / float(price),
        side=BUY,
        token_id=token_id,
    )
    signed_order = client.create_order(order_args)
    resp = client.post_order(signed_order)
    return resp


# ── CLI ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="PumaClaw Polymarket Trading Engine v2"
    )
    sub = parser.add_subparsers(dest="action")

    # list — fetch active markets
    p_list = sub.add_parser("list", help="List active markets")
    p_list.add_argument("--query", "-q", help="Text search filter")
    p_list.add_argument("--tag", "-t", help="Category tag (Politics, Crypto, Sports...)")
    p_list.add_argument("--limit", "-n", type=int, default=10, help="Number of results")
    p_list.add_argument(
        "--sort", "-s", default="volume_24hr",
        choices=["volume_24hr", "volume", "liquidity", "start_date", "end_date"],
        help="Sort order"
    )

    # tags — list available tags
    sub.add_parser("tags", help="List available market categories/tags")

    # price — get current price
    p_price = sub.add_parser("price", help="Get current price for a token")
    p_price.add_argument("token_id", help="Token ID to check")

    # bet — place an order
    p_bet = sub.add_parser("bet", help="Place a BUY order")
    p_bet.add_argument("--token", required=True, help="Outcome token ID")
    p_bet.add_argument("--amount", required=True, help="Amount in USDC")
    p_bet.add_argument("--price", required=True, help="Target price (0-1)")

    # status — API health check
    sub.add_parser("status", help="Check API connection")

    args = parser.parse_args()

    try:
        if args.action == "list":
            markets = fetch_active_markets(
                query=args.query, tag=args.tag,
                limit=args.limit, order=args.sort
            )
            print(json.dumps(markets, indent=2, ensure_ascii=False))

        elif args.action == "tags":
            tags = fetch_tags()
            print(json.dumps(tags, indent=2, ensure_ascii=False))

        elif args.action == "price":
            price = get_price(args.token_id)
            print(json.dumps(price, indent=2, ensure_ascii=False))

        elif args.action == "bet":
            resp = place_bet(args.token, args.amount, args.price)
            print(json.dumps(resp, indent=2, ensure_ascii=False))

        elif args.action == "status":
            client = get_clob_client(authenticated=False)
            print(json.dumps(client.get_ok(), indent=2, ensure_ascii=False))

        else:
            parser.print_help()

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
