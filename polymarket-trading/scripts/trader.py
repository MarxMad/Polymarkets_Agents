#!/usr/bin/env python3
"""
PumaClaw Autonomous Trader v2 — Full Research Agent
=====================================================
Multi-layer trading system inspired by the Polymarket Agentic Trading architecture.

Layer 0: Data Ingestion (Gamma API + Brave Search)
Layer 1: Research Agent (LLM probability estimation)
Layer 2: Signal Generation (Edge calc + Devil's Advocate + Confidence)
Layer 3: Portfolio & Risk (Kelly, max exposure, circuit breakers)
Layer 4: Execution (CLOB orders with random delays)
Layer 5: Monitoring (P&L tracking, trade log, Telegram notifications)

Usage:
    python3 trader.py                  # Run continuous daemon
    python3 trader.py --dry-run        # Simulate without real bets
    python3 trader.py --once           # Run one cycle and exit
"""
import argparse
import json
import logging
import os
import random
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

import requests
import tweepy
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON
from py_clob_client.clob_types import ApiCreds, OrderArgs, AssetType, BalanceAllowanceParams
from py_clob_client.order_builder.constants import BUY, SELL
from web3 import Web3

# ── Mission Critical Settings ───────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ── Paths ────────────────────────────────────────────────────────────────
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
STRATEGY_PATH = os.path.join(SKILL_DIR, "strategy.json")
TRADES_PATH = os.path.expanduser("~/.openclaw/workspace/trades.json")
CACHE_PATH = os.path.expanduser("~/.openclaw/workspace/research_cache.json")
LOG_PATH = "/tmp/openclaw/pumaclaw-trader.log"

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
BRAVE_API = "https://api.search.brave.com/res/v1/web/search"

# ── Models ───────────────────────────────────────────────────────────────
MODEL_FAST = "gpt-4o-mini"
MODEL_DEEP = "gpt-4o"

# ── Logging ──────────────────────────────────────────────────────────────
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("pumaclaw-trader")

# ── Graceful shutdown ────────────────────────────────────────────────────
_running = True

def _shutdown(signum, frame):
    global _running
    log.info("Shutdown signal received.")
    _running = False

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT, _shutdown)


# ── Helpers ──────────────────────────────────────────────────────────────

def load_strategy():
    with open(STRATEGY_PATH) as f:
        return json.load(f)

def save_strategy(strategy):
    with open(STRATEGY_PATH, "w") as f:
        json.dump(strategy, f, indent=2)

def load_trades():
    if os.path.exists(TRADES_PATH):
        with open(TRADES_PATH) as f:
            return json.load(f)
    return {"trades": [], "stats": {"total_wagered": 0, "wins": 0, "losses": 0, "pending": 0}}

def save_trades(trades):
    with open(TRADES_PATH, "w") as f:
        json.dump(trades, f, indent=2, ensure_ascii=False)

def load_cache():
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                return json.load(f)
        except: pass
    return {}

def save_cache(cache):
    # Prune old entries (> 1 hour) to keep it small
    now = time.time()
    clean = {k: v for k, v in cache.items() if now - v.get("ts", 0) < 3600}
    with open(CACHE_PATH, "w") as f:
        json.dump(clean, f, indent=2)

def _is_valid_hex_key(key):
    if not key:
        return False
    clean = key.lstrip("0x")
    try:
        int(clean, 16)
        return len(clean) >= 32
    except ValueError:
        return False

def get_clob_client():
    pk = os.getenv("POLYMARKET_PRIVATE_KEY")
    api_key = os.getenv("POLYMARKET_API_KEY")
    api_secret = os.getenv("POLYMARKET_API_SECRET")
    api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
    if not _is_valid_hex_key(pk):
        raise Exception("Invalid POLYMARKET_PRIVATE_KEY")
    
    # 0x1294... is the Proxy address from the user's Polymarket settings
    # We use signature_type=2 for certain Proxy/Safe accounts (try 1 then 2)
    PROXY_ADDRESS = "0x1294d2B89B08E8651124F04534FB2715a1437846"
    creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=api_passphrase) if api_key else None
    return ClobClient(CLOB_HOST, key=pk, chain_id=POLYGON, creds=creds, signature_type=2, funder=PROXY_ADDRESS)


def get_wallet_balance():
    """Fetch real USDC.e balance from the Proxy wallet on Polygon."""
    # Use a more reliable RPC
    rpc_url = os.getenv("POLYGON_RPC_URL", "https://1rpc.io/matic")
    usdc_e_address = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    # This is the address that currently holds the USDC.e (Polymarket Proxy)
    proxy_address = "0x1294d2B89B08E8651124F04534FB2715a1437846"
    
    abi = [{"constant": True, "inputs": [{"name": "_owner", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"}]
    
    try:
        w3 = Web3(Web3.HTTPProvider(rpc_url))
        if not w3.is_connected():
            return None
        contract = w3.eth.contract(address=Web3.to_checksum_address(usdc_e_address), abi=abi)
        balance_raw = contract.functions.balanceOf(Web3.to_checksum_address(proxy_address)).call()
        return float(balance_raw) / 1e6 # USDC.e is always 6 decimals
    except Exception as e:
        log.error(f"Error fetching balance: {e}")
        return None


def heal_bot(error_msg):
    """Bridge to Claude Code for autonomous engineering and self-healing."""
    log.info("🐆 SELF-HEALING: Invoking Claude Code to analyze the crash...")
    
    # Capture recent logs for context
    try:
        with open(LOG_PATH, "r") as f:
            lines = f.readlines()
            logs_context = "".join(lines[-20:])
    except:
        logs_context = "Could not read logs."

    prompt = f"""
    The PumaClaw trader crashed with this error: {error_msg}
    
    Recent logs context:
    {logs_context}
    
    Please analyze scripts/trader.py and suggest a fix. Be precise.
    """
    
    try:
        # Run Claude Code CLI in non-interactive mode
        # env ensures the API key is available
        env = os.environ.copy()
        if "ANTHROPIC_API_KEY" not in env:
             # Try to load from .env on VPS
             env["ANTHROPIC_API_KEY"] = os.getenv("ANTHROPIC_API_KEY", "")

        cmd = ["claude", "-p", prompt]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=60)
        
        fix_suggestion = result.stdout if result.returncode == 0 else result.stderr
        log.info(f"🐆 CLAUDE FIX SUGGESTION:\n{fix_suggestion}")
        
        # Notify user via Telegram
        msg = f"🐆 **PumaClaw Self-Healing Alert** 🩹\n\nEl bot crasheó con: `{error_msg}`\n\n**Análisis de Claude Code**:\n{fix_suggestion[:3000]}..."
        notify_telegram(msg)
        
    except Exception as e:
        log.error(f"Failed to invoke Claude Code: {e}")

# ══════════════════════════════════════════════════════════════════════════
# LAYER 0 — DATA INGESTION
# ══════════════════════════════════════════════════════════════════════════

def scan_markets(strategy):
    """Fetch active markets from Gamma API filtered by strategy tags."""
    all_markets = []
    
    # Track unique condition IDs to avoid duplicates
    seen_ids = set()

    # Binary Sniper Pivot: We skip broad tag scans to focus exclusively 
    # on ultra-short-term binary markets as requested by the user.
    log.info("L0: Binary Sniper Mode active — focusing on 5/15 min markets.")

    # Step 2: Targeted Tag Discovery for "Binary" markets
    # Uses Tag IDs 102892 (5M) and 102127 (Up/Down) for direct discovery.
    # We loop each instead of sending a list to ensure Gamma API compatibility.
    binary_tags = [102892, 102127]
    for tid in binary_tags:
        try:
            log.info(f"L0: Scanning specialized tag {tid} for binary markets...")
            params = {"active": "true", "closed": "false", "limit": 50, "tag_id": tid}
            resp = requests.get(f"{GAMMA_API}/events", params=params, timeout=15)
            if resp.status_code == 200:
                for event in resp.json():
                    for mkt in event.get("markets", []):
                        cid = mkt.get("conditionId", "")
                        if cid in seen_ids or not mkt.get("active") or not mkt.get("acceptingOrders"):
                            continue
                        
                        series_data = event.get("series", [{}])[0] if event.get("series") else {}
                        vol24 = float(event.get("volume24hr") or series_data.get("volume24hr") or mkt.get("volume24hr") or 0)
                        liq = float(event.get("liquidity") or series_data.get("liquidity") or mkt.get("liquidity") or 0)
                        
                        # 5M recurrent snipers usually spawn with 0 liquidity until the market maker
                        # provides orders right before the window. We bypass the strict check here.
                        pass  # Let the orderbook execution layer test liquidity

                        try:
                            prices = json.loads(mkt.get("outcomePrices", "[]"))
                            token_ids = json.loads(mkt.get("clobTokenIds", "[]"))
                            if len(prices) < 2 or len(token_ids) < 2: continue
                            
                            all_markets.append({
                                "event_title": event.get("title", ""),
                                "question": mkt.get("question", ""),
                                "description": mkt.get("description", "")[:500],
                                "tag": "Binary",
                                "volume_24h": vol24,
                                "liquidity": liq,
                                "yes_price": float(prices[0]),
                                "no_price": float(prices[1]),
                                "yes_token": token_ids[0],
                                "no_token": token_ids[1],
                                "condition_id": cid,
                                "end_date": mkt.get("endDate", ""),
                                "url": f"https://polymarket.com/event/{event.get('slug', '')}",
                            })
                            seen_ids.add(cid)
                        except: continue
        except Exception as e:
            log.warning(f"Tag discovery for {tid} failed: {e}")

    # Step 2.5: Targeted Keyword Search
    search_queries = ["5 min", "15 min", "Bitcoin Price"]
    for query in search_queries:
        try:
            log.info(f"L0: Performing targeted search for '{query}'...")
            params = {"active": "true", "closed": "false", "limit": 40, "query": query}
            resp = requests.get(f"{GAMMA_API}/events", params=params, timeout=15)
            if resp.status_code == 200:
                for event in resp.json():
                    for mkt in event.get("markets", []):
                        cid = mkt.get("conditionId", "")
                        if cid in seen_ids or not mkt.get("active") or not mkt.get("acceptingOrders"):
                            continue
                        
                        series_data = event.get("series", [{}])[0] if event.get("series") else {}
                        vol24 = float(event.get("volume24hr") or series_data.get("volume24hr") or mkt.get("volume24hr") or 0)
                        liq = float(event.get("liquidity") or series_data.get("liquidity") or mkt.get("liquidity") or 0)
                        
                        pass # Bypass strict checks for binary snipers

                        try:
                            prices = json.loads(mkt.get("outcomePrices", "[]"))
                            token_ids = json.loads(mkt.get("clobTokenIds", "[]"))
                            if len(prices) < 2 or len(token_ids) < 2: continue
                            
                            all_markets.append({
                                "event_title": event.get("title", ""),
                                "question": mkt.get("question", ""),
                                "description": mkt.get("description", "")[:500],
                                "tag": "Binary",
                                "volume_24h": vol24,
                                "liquidity": liq,
                                "yes_price": float(prices[0]),
                                "no_price": float(prices[1]),
                                "yes_token": token_ids[0],
                                "no_token": token_ids[1],
                                "condition_id": cid,
                                "end_date": mkt.get("endDate", ""),
                                "url": f"https://polymarket.com/event/{event.get('slug', '')}",
                            })
                            seen_ids.add(cid)
                        except: continue
        except Exception as e:
            log.warning(f"Keyword search for '{query}' failed: {e}")

    # Step 3: Filter by resolution date and Priority
    max_days = strategy.get("max_days_to_resolution", 7)
    now = datetime.now(timezone.utc)
    filtered = []

    for m in all_markets:
        try:
            end_dt = datetime.fromisoformat(m["end_date"].replace("Z", "+00:00"))
            diff = end_dt - now
            days_left = diff.days
            secs_left = diff.total_seconds()
            
            # 🐆 BINARY SNIPER STRIKE 🐆
            # Strictly verify this is a 5/15 minute market via URL slug or question text.
            q_lower = m["question"].lower()
            t_lower = m["event_title"].lower()
            u_lower = m.get("url", "")
            is_strict_binary = any(kw in q_lower or kw in t_lower or kw in u_lower for kw in ["5 min", "15 min", "-5m-", "-15m-", "5m ", "15m "])
            is_crypto = any(kw in q_lower or kw in t_lower or kw in u_lower for kw in ["bitcoin", "btc", "ethereum", "eth", "solana", "sol", "xrp", "crypto"])

            if is_strict_binary and is_crypto:
                m["is_velocity"] = True # All 5/15m are velocity
                filtered.append(m)
            else:
                continue

        except:
            continue

    # 🐆 DIVERSIFY THE HUNT 🐆
    random.shuffle(filtered)
    filtered.sort(key=lambda x: x.get("is_velocity", False), reverse=True)

    log.info(f"L0: Found {len(filtered)} BINARY markets ({len([m for m in filtered if m.get('is_velocity')])} velocity).")
    return filtered[:30] 


def search_news(query, num_results=5):
    """Search Brave for recent news about a market topic."""
    api_key = os.getenv("BRAVE_API_KEY")
    if not api_key:
        return []
    try:
        headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
        params = {"q": query, "count": num_results, "freshness": "pw"}  # past week
        resp = requests.get(BRAVE_API, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", [])[:num_results]:
            results.append({
                "title": item.get("title", ""),
                "description": item.get("description", ""),
                "url": item.get("url", ""),
                "age": item.get("age", ""),
            })
        return results
    except Exception as e:
        log.warning(f"Brave search failed: {e}")
        return []


# ── Phase 3 — Twitter Sentiment ──────────────────────────────────────────

def get_twitter_sentiment(query):
    """Fetch recent tweets and analyze social sentiment."""
    log.info(f"L0: Checking X/Twitter sentiment for '{query}'...")
    
    # Credentials from .env
    consumer_key = os.getenv("TWITTER_CONSUMER_KEY")
    consumer_secret = os.getenv("TWITTER_CONSUMER_SECRET")
    access_token = os.getenv("TWITTER_ACCESS_TOKEN")
    access_secret = os.getenv("TWITTER_ACCESS_SECRET")
    bearer_token = os.getenv("TWITTER_BEARER_TOKEN")
    
    if not bearer_token:
        log.warning("No TWITTER_BEARER_TOKEN — skipping social sentiment")
        return []

    try:
        # Use Client (v2 API) for search
        client = tweepy.Client(bearer_token=bearer_token)
        
        # Sanitize query for Twitter API (remove $, brackets, extra spaces)
        clean_query = query.replace("$", "").replace("(", "").replace(")", "").replace("?", "").replace(">", "").replace("<", "").replace("=", "")
        search_query = f"{clean_query} -is:retweet lang:en"
        
        log.info(f"L0: Final sanitized X query: '{search_query}'")
        response = client.search_recent_tweets(query=search_query[:512], max_results=10)
        
        results = []
        if response.data:
            for tweet in response.data:
                results.append(tweet.text[:200]) # Just text snippets
        
        return results
    except Exception as e:
        log.warning(f"Twitter API failed: {e}")
        return []


# ══════════════════════════════════════════════════════════════════════════
# LAYER 1 — RESEARCH AGENT (LLM Probability Estimation)
# ══════════════════════════════════════════════════════════════════════════

def estimate_probability(market, news_context, social_context=None, model=MODEL_FAST):
    """
    Use OpenAI LLM to estimate the true probability of a market outcome.
    Incorporates both News (Brave) and Sentiment (Twitter).
    """
    openai_key = os.getenv("OPENAI_API_KEY")
    if not openai_key:
        log.warning("No OPENAI_API_KEY — skipping LLM research")
        return None

    # Format News
    news_text = "\n".join([f"- {n['title']}: {n['description']}" for n in news_context[:5]]) if news_context else "No recent news."
    
    # Format Social Sentiment
    social_text = "\n".join([f"- {s}" for s in social_context[:5]]) if social_context else "No real-time social pulse available."

    velocity_note = ""
    if market.get("is_velocity"):
        velocity_note = "CRITICAL: This market resolves VERY SOON (high-velocity). Ignore long-term fundamentals. Focus ONLY on current momentum, social hype, and breaking news."

    prompt = f"""You are a high-frequency prediction market analyst. Estimate the TRUE probability that this event resolves YES. 
{velocity_note}

MARKET: {market['question']}
DESCRIPTION: {market['description'][:300]}
END DATE: {market['end_date']}
CURRENT MARKET PRICE: Yes = ${market['yes_price']:.3f}, No = ${market['no_price']:.3f}

--- NEWS CONTEXT ---
{news_text}

--- SOCIAL MEDIA PULSE (X/Twitter) ---
{social_text}

Instructions:
1. Compare official news with social sentiment. Is there a "pump" or "FUD" in progress?
2. Estimate the TRUE probability (0.0 to 1.0) that this resolves YES.
3. Rate your confidence (0.0 to 1.0).
4. Briefly explain your reasoning (max 1 sentence).

Respond ONLY in this exact JSON format:
{{"yes_prob": 0.XX, "confidence": 0.XX, "reasoning": "..."}}"""

    try:
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {openai_key}", "Content-Type": "application/json"},
            json={
                "model": model, 
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 200,
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()

        # Parse JSON from response (handle markdown wrapping)
        if "```" in content:
            content = content.split("```")[1].strip()
            if content.startswith("json"):
                content = content[4:].strip()

        result = json.loads(content)
        yes_prob = float(result.get("yes_prob", 0.5))
        confidence = float(result.get("confidence", 0.5))
        reasoning = result.get("reasoning", "No reasoning provided")

        # Clamp values
        yes_prob = max(0.01, min(0.99, yes_prob))
        confidence = max(0.0, min(1.0, confidence))

        return {"yes_prob": yes_prob, "confidence": confidence, "reasoning": reasoning}

    except Exception as e:
        log.warning(f"LLM research failed: {e}")
        return None


# ── Phase 3.2 — Scalper Engine ───────────────────────────────────────────

def get_token_price(token_id):
    """Fetch current mid-price for a token on the CLOB."""
    try:
        # Fallback to direct HTTP first as it's most reliable for quick price 
        resp = requests.get(f"https://clob.polymarket.com/price?token_id={token_id}&side=buy", timeout=5)
        if resp.status_code == 200:
            return float(resp.json().get("price", 0.5))
        
        # If that fails, try the market endpoint
        client = get_clob_client()
        market = client.get_market(token_id)
        if market and 'cur_price' in market:
            return float(market['cur_price'])
    except Exception as e:
        log.warning(f"Price fetch failed for {token_id[:8]}: {str(e)[:50]}")
    return None # Return None to skip exposure/scalp logic if price is unknown

def check_scalp_opportunities(strategy, trades_log, dry_run=False):
    """Scan open positions and SELL if profit target met (Take Profit)."""
    log.info("L6: Checking Scalp/Take-Profit opportunities...")
    client = get_clob_client()
    
    # Identify active positions
    active_tids = set()
    token_entry_prices = {} 
    for t in trades_log.get("trades", []):
        if t.get("status") == "executed" and "token_id" in t:
            active_tids.add(t["token_id"])
            token_entry_prices[t["token_id"]] = t.get("price", 0.5)

    for tid in active_tids:
        try:
            # Fix: Use BalanceAllowanceParams object
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
            b_info = client.get_balance_allowance(params)
            # Fix: Polymarket uses 6 decimals for shares/USDC.e
            balance = float(b_info.get("balance", 0)) / 1e6
            if balance < 1.0: continue 
            
            curr_price = get_token_price(tid)
            if not curr_price: continue
            
            entry = token_entry_prices.get(tid, 0.5)
            multiplier = curr_price / entry if entry > 0 else 0
            
            # Scalping Logic: If price >= 3x entry (default), or >= 0.92, SELL
            target_multiplier = strategy.get("scalp_multiplier", 3.0) 
            if multiplier >= target_multiplier or curr_price >= 0.92:
                log.info(f"🏹 SCALP ALERT: Multiplier {multiplier:.1f}x (Current: {curr_price:.2f})")
                if dry_run:
                    log.info(f"  [DRY-SCALP] Would SELL {balance:.1f} shares @ {curr_price:.3f}")
                else:
                    order_args = OrderArgs(price=curr_price * 0.98, size=balance, side=SELL, token_id=tid)
                    signed = client.create_order(order_args)
                    resp = client.post_order(signed)
                    log.info(f"  [LIVE-SCALP] SOLD {balance:.1f} shares @ {curr_price:.3f}")
                    trades_log["trades"].append({
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "event": "SCALP / TAKE PROFIT",
                        "side": "SELL",
                        "price": curr_price,
                        "amount_usd": balance * curr_price,
                        "status": "executed",
                        "token_id": tid,
                        "reasoning": f"Take Profit @ {multiplier:.1f}x profit"
                    })
        except Exception as e:
            log.warning(f"Scalp check failed for {tid[:8]}: {e}")


# ══════════════════════════════════════════════════════════════════════════
# LAYER 2 — SIGNAL GENERATION
# ══════════════════════════════════════════════════════════════════════════

def generate_signals(markets, strategy):
    """
    For each market, run research and generate trading signals.
    Only markets with edge > threshold AND confidence >= 60% pass.
    """
    signals = []
    min_confidence = strategy.get("min_confidence", 0.60)
    min_edge = strategy["min_ev_threshold"]
    min_p = strategy["min_price"]
    max_p = strategy["max_price"]

    # Pre-filter: only consider markets with prices in range
    candidates = []
    for mkt in markets:
        yp, np = mkt["yes_price"], mkt["no_price"]
        if (min_p <= yp <= max_p) or (min_p <= np <= max_p):
            # Score by volume/liquidity ratio for prioritization
            vlr = mkt["volume_24h"] / mkt["liquidity"] if mkt["liquidity"] > 0 else 0
            mkt["vlr"] = vlr
            candidates.append(mkt)

    # Sort by Velocity (priority) then VLR
    candidates.sort(key=lambda x: (x.get("is_velocity", False), x.get("vlr", 0)), reverse=True)
    candidates = candidates[:20] # Increased for high-velocity focus

    log.info(f"L2: Analyzing {len(candidates)} candidate markets with LLM...")

    research_cache = load_cache()
    now = time.time()
    cache_ttl = 1800 # 30 minutes

    seen_conditions = set()
    for mkt in candidates:
        cid = mkt["condition_id"]
        if cid in seen_conditions:
            continue
        seen_conditions.add(cid)

        # ── Check Cache ──
        rs_news = []      # Robust initialization
        rs_social = None  # Robust initialization
        cached = research_cache.get(cid)
        
        if cached and (now - cached.get("ts", 0)) < cache_ttl:
             log.info(f"L2: Using cached research for {mkt['question'][:40]}...")
             research = cached["data"]
        else:
            # ⚡ VELOCITY SNIPER BYPASS ⚡
            # For 5-min markets or high-velocity, skip slow external searches
            is_ultra_fast = mkt.get("is_velocity") and ("5 min" in mkt["question"].lower() or "15 min" in mkt["question"].lower())
            
            if is_ultra_fast:
                log.info(f"⚡ L2: Velocity Sniper Active — Bypassing slow research for '{mkt['question'][:30]}'")
                rs_news = []
                rs_social = []
            else:
                # L0: Search recent news + Twitter Sentiment
                search_query = mkt["question"][:80]
                rs_news = search_news(search_query, num_results=3)
                rs_social = get_twitter_sentiment(search_query) 
                time.sleep(0.5)  # Rate limit

            # L1: LLM probability estimation
            research = estimate_probability(mkt, rs_news, social_context=rs_social, model=MODEL_FAST)
            if not research:
                continue
            

        yes_prob = research["yes_prob"]
        confidence = research["confidence"]
        side = "Yes" if yes_prob > mkt["yes_price"] else "No"
        side_price = mkt["yes_price"] if side == "Yes" else mkt["no_price"]
        edge = abs(yes_prob - side_price)

        # Sniper Mode: If shares are hyper-cheap (0.1c - 5c), verify quality
        is_sniper = side_price < 0.05
        if is_sniper:
            if confidence < 0.65: # Higher bar for sniper shots
                continue
            log.info(f"🎯 [SNIPER] Detecting inefficiencies in {mkt['question'][:40]}... (Price: {side_price:.3f})")

        # 2. Deep Validation Layer (Pro)
        # Skip deep validation for velocity markets (too slow!)
        if edge > 0.10 and research["confidence"] >= 0.7 and not mkt.get("is_velocity"):
            log.info(f"L2: High-potential signal found ({edge:.1%}). Triggering Deep Validation...")
            deep_research = estimate_probability(mkt, rs_news, social_context=rs_social, model=MODEL_DEEP)
            if deep_research:
                log.info(f"L2: Deep Validation result: {deep_research['yes_prob']:.2f}")
                research = deep_research
                yes_prob = research["yes_prob"]
                side = "Yes" if yes_prob > mkt["yes_price"] else "No"
                side_price = mkt["yes_price"] if side == "Yes" else mkt["no_price"]
                edge = abs(yes_prob - side_price)

        if edge < strategy["min_ev_threshold"] or research["confidence"] < strategy["min_confidence"]:
            continue

        llm_yes = research["yes_prob"]
        llm_no = 1.0 - llm_yes
        confidence = research["confidence"]

        # Calculate edge for each side
        for side, mkt_price, llm_prob, token in [
            ("Yes", mkt["yes_price"], llm_yes, mkt["yes_token"]),
            ("No", mkt["no_price"], llm_no, mkt["no_token"]),
        ]:
            if mkt_price < min_p or mkt_price > max_p:
                continue

            # Edge = LLM probability - market price
            edge = llm_prob - mkt_price

            if edge < min_edge:
                continue

            # Kelly sizing
            odds = 1.0 / mkt_price if mkt_price > 0 else 0
            kelly_pct = edge / (odds - 1) if odds > 1 else 0
            kelly_pct = max(0, min(kelly_pct, 0.3))
            fractional_kelly = kelly_pct * strategy["kelly_fraction"]

            if fractional_kelly < 0.005:
                continue

            ev = edge * (odds - 1) if odds > 1 else 0

            signals.append({
                **mkt,
                "side": side,
                "side_price": mkt_price,
                "side_token": token,
                "edge": round(edge, 4),
                "kelly_pct": round(fractional_kelly, 4),
                "ev": round(ev, 4),
                "llm_prob": round(llm_prob, 3),
                "confidence": round(confidence, 2),
                "reasoning": research["reasoning"],
                "news_count": len(rs_news),
            })

    # Sort by EV descending
    signals.sort(key=lambda x: x["ev"], reverse=True)
    log.info(f"L2: Generated {len(signals)} validated signals")
    
    # NEW: Save cache at the end of the market analysis loop to optimize I/O
    save_cache(research_cache)
    
    return signals[:5]


# ══════════════════════════════════════════════════════════════════════════
# LAYER 3 — PORTFOLIO & RISK
# ══════════════════════════════════════════════════════════════════════════

def apply_risk_limits(signals, strategy, trades_log):
    """Apply portfolio-level risk constraints based on REAL token balances."""
    approved = []
    bankroll = strategy["bankroll_usd"]
    max_bet = bankroll * strategy["max_bet_pct"]
    
    # Track dynamic exposure from real-time balances
    active_exposure = 0
    exposure_by_event = {} # question_title -> exposure_usd
    
    log.info("L3: Calculating real-time exposure from token balances...")
    client = get_clob_client()
    
    # Track which events we are already in by scanning ALL historical tokens in trades.json
    token_metadata = {} # token_id -> {tag, event_title}
    for t in trades_log.get("trades", []):
        if "token_id" in t:
            # We track ALL tokens we've ever touched to find phantom exposure
            token_metadata[t["token_id"]] = {"tag": t.get("tag", "Crypto"), "event": t.get("event", t.get("event_title", ""))}

    for tid, meta in token_metadata.items():
        try:
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
            b_info = client.get_balance_allowance(params)
            qty = float(b_info.get("balance", 0)) / 1e6
            if qty > 0.05: # Only count significant holdings
                p_info = get_token_price(tid)
                price = p_info if p_info is not None else 0.0
                value = (qty * price)
                active_exposure += value
                
                evt = meta["event"]
                if evt:
                    exposure_by_event[evt] = exposure_by_event.get(evt, 0) + value
        except: continue

    log.info(f"L3: Current Real-Time Exposure: ${active_exposure:.2f} across {len(exposure_by_event)} events")

    # Circuit breaker: use TOTAL portfolio value (Balance + Exposure)
    portfolio_value = bankroll + active_exposure
    peak_portfolio = strategy.get("peak_portfolio", portfolio_value)

    # Limits (now relative to TOTAL portfolio value, not just liquid bankroll)
    max_total_exposure = portfolio_value * 0.85 
    max_tag_exposure = portfolio_value * 0.40   
    
    if portfolio_value < peak_portfolio * 0.5:
        log.warning(f"L3: CIRCUIT BREAKER — Portfolio ${portfolio_value:.2f} < 50% of peak ${peak_portfolio:.2f}")
        return []
    
    # Update peak portfolio if we reached a new high
    if portfolio_value > peak_portfolio:
        strategy["peak_portfolio"] = portfolio_value

    for sig in signals:
        is_velocity = sig.get("is_velocity", False)
        
        # Velocity markets get their own high-limit bucket
        if is_velocity:
            tag_limit = portfolio_value * 0.70 # Velocity markets can take more share
        else:
            tag_limit = max_tag_exposure

        # For now, we use a simplified global + per-signal check since real-time
        # tag-specific balance tracking is expensive API-wise.
        # Rule 1: Per-Market Concentration Cap (Max 20% of portfolio per event)
        event_title = sig.get("event_title", "")
        # Find similar event titles to group exposure (e.g. MegaETH No and MegaETH >$1B)
        market_exposure = 0
        for evt, val in exposure_by_event.items():
            if event_title.split()[0].lower() in evt.lower(): # Basic fuzzy match like "MegaETH"
                market_exposure += val
                
        # 🛡️ FIX: Also account for exposure from signals already approved in THIS cycle
        for asig in approved:
             if event_title.split()[0].lower() in asig["event_title"].lower():
                 market_exposure += asig["bet_size"]

        # EXTREME DIVERSIFICATION: $1 USD Max Exposure for small accounts
        exposure_cap = portfolio_value * 0.20
        if bankroll < 100:
            exposure_cap = 1.00 # CAP $1.00 USD strictly

        if market_exposure >= exposure_cap:
             log.info(f"L3: Skip {event_title[:30]} (Market Exposure ${market_exposure:.2f} >= ${exposure_cap:.2f})")
             continue

        # Rule 2: Global Signal Cap per cycle (Max 3 signals for small accounts)
        global_cap = 3 if bankroll < 100 else 4
        if len(approved) >= global_cap:
            log.info(f"L3: Global cycle cap reached ({global_cap} signals). Stopping.")
            break

        # Cap bet size
        min_shares = 5 # Polymarket CLOB hard minimum
        min_usd = 0.05
        
        # Rule 2: Strict Bet Cap for Small Accounts
        # Base bet from Kelly
        bet_usd = bankroll * sig["kelly_pct"]
        
        # Capping for Small Accounts (<$100): 
        # Target ~ $0.50 - $1.00 per trade to force diversification
        if bankroll < 100:
            # Force ultra-conservative Kelly (0.1) for small accounts
            bet_usd = bankroll * (sig["kelly_pct"] * 0.2) 
            bet_usd = min(bet_usd, 1.00) # Strict $1.00 cap
        else:
            bet_usd = min(bet_usd, max_bet)

        shares = bet_usd / sig["side_price"]

        if shares < min_shares:
            # Force the 5-share minimum ONLY if we have VERY high edge and total bankroll allows it
            mkt_min_usd = min_shares * sig["side_price"]
            if sig["edge"] > 0.15 and mkt_min_usd <= bankroll * 0.25:
                shares = min_shares
                bet_usd = mkt_min_usd
            else:
                log.info(f"L3: Skip {sig['question'][:30]}... (Size {shares:.2f} < 5 shares or cost ${mkt_min_usd:.2f} too high)")
                continue
        
        # Final Rounding & Compliance
        shares = max(shares, min_shares)
        bet_usd = shares * sig["side_price"]

        if bet_usd < min_usd:
             continue

        if bet_usd > bankroll * 0.4: # Hard safety ceiling per trade
            bet_usd = bankroll * 0.4
            shares = bet_usd / sig["side_price"]

        sig["bet_size"] = round(bet_usd, 4)
        sig["shares"] = round(shares, 2)
        approved.append(sig)

    log.info(f"L3: Approved {len(approved)}/{len(signals)} signals after risk limits")
    return approved


# ══════════════════════════════════════════════════════════════════════════
# LAYER 4 — EXECUTION
# ══════════════════════════════════════════════════════════════════════════

def execute_trade(sig, strategy, dry_run=False):
    """Execute a BUY order on Polymarket."""
    bet_size = sig["bet_size"]

    trade_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": sig["event_title"],
        "question": sig["question"],
        "side": sig["side"],
        "price": sig["side_price"],
        "amount_usd": bet_size,
        "edge": sig["edge"],
        "ev": sig["ev"],
        "llm_prob": sig.get("llm_prob", 0),
        "confidence": sig.get("confidence", 0),
        "reasoning": sig.get("reasoning", ""),
        "kelly_pct": sig["kelly_pct"],
        "token_id": sig["side_token"],
        "condition_id": sig["condition_id"],
        "url": sig["url"],
        "tag": sig["tag"],
        "dry_run": dry_run,
        "status": "pending",
    }

    if dry_run:
        trade_record["status"] = "simulated"
        log.info(f"  [DRY] ${bet_size:.2f} on {sig['side']} @ {sig['side_price']:.3f} | "
                 f"Edge: {sig['edge']:.1%} | Conf: {sig.get('confidence',0):.0%} | {sig['question'][:50]}")
    else:
        try:
            client = get_clob_client()
            order_args = OrderArgs(
                price=float(sig["side_price"]),
                size=float(sig["shares"]),
                side=BUY,
                token_id=sig["side_token"],
            )
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order)
            trade_record["status"] = "executed"
            trade_record["order_response"] = str(resp)[:200]
            log.info(f"  [LIVE] ${bet_size:.2f} on {sig['side']} @ {sig['side_price']:.3f} | "
                     f"Edge: {sig['edge']:.1%} | {sig['question'][:50]}")
        except Exception as e:
            trade_record["status"] = "error"
            trade_record["error"] = str(e)[:200]
            log.error(f"  Trade failed: {e}")

    return trade_record


# ══════════════════════════════════════════════════════════════════════════
# LAYER 5 — MONITORING & NOTIFICATIONS
# ══════════════════════════════════════════════════════════════════════════



def _send_tg(url, payload):
    try:
        r = requests.post(url, json=payload, timeout=20)
        if r.status_code != 200:
            log.warning(f"Telegram API Error: {r.status_code} - {r.text}")
        else:
            log.info(f"Telegram message sent successfully (ID: {payload.get('chat_id')})")
    except Exception as e:
        log.warning(f"Telegram POST failed: {e}")

def notify_telegram(message):
    """Send direct notification to user via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    if len(message) > 4000:
        chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
        for chunk in chunks:
            _send_tg(url, {"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "Markdown"})
    else:
        _send_tg(url, {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "Markdown"})


def build_report(executed, strategy, dry_run):
    """Build a human-readable Telegram report."""
    mode = "🧪 SIMULACIÓN" if dry_run else "🔥 LIVE"
    lines = [f"🐆 PumaClaw Trader — {mode}", ""]

    for t in executed:
        emoji = {"Crypto": "📊", "Politics": "🏛️", "Sports": "⚽"}.get(t["tag"], "📊")
        lines.append(f"{emoji} **{t['side']}** @ ${t['price']:.3f} → ${t['amount_usd']:.2f}")
        lines.append(f"   {t['question'][:70]}")
        lines.append(f"   Edge: {t['edge']:.1%} | Conf: {t.get('confidence',0):.0%} | LLM: {t.get('llm_prob',0):.0%}")
        lines.append(f"   💡 {t.get('reasoning', 'N/A')[:100]}")
        lines.append("")

    lines.append(f"💰 Bankroll: ${strategy['bankroll_usd']:.2f}")
    stats = load_trades().get("stats", {})
    lines.append(f"📈 Total wagered: ${stats.get('total_wagered', 0):.2f}")
    return "\n".join(lines)

def build_radar_report(signals, strategy):
    """Notify user about 'tasty' binary opportunities, prioritizing Crypto."""
    lines = [
        "🐆 **PumaRadar — Binary Crypto Sniper** 🍖🎯📡",
        f"💰 Balance: ${strategy['bankroll_usd']:.2f}",
        ""
    ]
    
    # Priority 1: Crypto Binary (5 min / 15 min / Price)
    crypto_keys = ['bitcoin', 'ethereum', 'solana', 'btc', 'eth', 'sol', 'price']
    crypto_sigs = [s for s in signals if any(kw in s['question'].lower() or kw in s['event_title'].lower() for kw in crypto_keys)]
    other_sigs = [s for s in signals if s not in crypto_sigs]
    
    display_signals = (crypto_sigs + other_sigs)[:5]

    if not display_signals:
        lines.append("🌕 El mercado de binarias está tranquilo. Esperando el siguiente tick...")
    else:
        lines.append("🔥 Top 5 Presas Crypto (Binarias 5/15m):")
        for i, s in enumerate(display_signals):
            side_color = "🟢" if s['side'] == "Yes" else "🔴"
            time_left = "⏱️ Velocity" if s.get('is_velocity') else "⏳ Normal"
            lines.append(f"{i+1}. {s['question'][:60]}...")
            lines.append(f"   {side_color} {s['side']} @ {s['side_price']:.3f} | Edge: {s['edge']:.1%} | {time_left}")
    
    lines.extend([
        "",
        "💡 *Enfoque exclusivo: Mercados de 5/15 min. Riesgo controlado ($1 max).*"
    ])
    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════════════════
# MAIN LOOP
# ══════════════════════════════════════════════════════════════════════════

def run_cycle(strategy, dry_run=False):
    """Run one complete scan → research → signal → risk → execute cycle."""
    log.info("=" * 60)
    
    # Dynamically update bankroll from wallet
    real_balance = get_wallet_balance()
    if real_balance is not None:
        log.info(f"💰 Dynamic Bankroll: ${real_balance:.2f} (Updated from Wallet)")
        strategy["bankroll_usd"] = real_balance
        if real_balance > strategy.get("peak_bankroll", 0):
            strategy["peak_bankroll"] = real_balance
    else:
        log.warning(f"Using cached Bankroll: ${strategy['bankroll_usd']:.2f}")

    log.info(f"Cycle start | Bankroll: ${strategy['bankroll_usd']:.2f} | Dry: {dry_run}")

    trades_log = load_trades()

    # Phase 3.2: Scalper check (before searching for new trades)
    check_scalp_opportunities(strategy, trades_log, dry_run=dry_run)

    # L0: Scan markets
    markets = scan_markets(strategy)
    signals = []
    if markets:
        # L2: Generate signals (includes L0 news search + L1 LLM research)
        signals = generate_signals(markets, strategy)
    else:
        log.info("No qualifying markets.")

    # L3: Apply risk limits
    trades_log = load_trades()
    approved = apply_risk_limits(signals, strategy, trades_log)
    
    # L4: Execute trades
    executed = []
    if approved:
        for i, sig in enumerate(approved):
            if i > 0:
                delay = random.uniform(5, 20)
                log.info(f"  Delay {delay:.0f}s...")
                time.sleep(delay)

            trade = execute_trade(sig, strategy, dry_run=dry_run)
            if trade:
                trades_log["trades"].append(trade)
                if trade["status"] in ("executed", "simulated"):
                    executed.append(trade)
                    if not dry_run and trade["status"] == "executed":
                        strategy["bankroll_usd"] -= trade["amount_usd"]
                        trades_log["stats"]["total_wagered"] += trade["amount_usd"]
                        if "pending" not in trades_log["stats"]:
                            trades_log["stats"]["pending"] = 0
                        trades_log["stats"]["pending"] += 1
    else:
        log.info("L3: All signals rejected by risk limits (diversification or bankroll).")

    save_trades(trades_log)
    if not dry_run:
        # Track peak bankroll for circuit breaker
        if strategy["bankroll_usd"] > strategy.get("peak_bankroll", 0):
            strategy["peak_bankroll"] = strategy["bankroll_usd"]
        save_strategy(strategy)

    # L5: Notify
    # Mandatorily send Radar Report every cycle if no trades happened
    if executed:
        report = build_report(executed, strategy, dry_run)
        notify_telegram(report)
    else:
        # Radar Mode: Tell the user what we found even if we didn't bet
        log.info("L5: No trades executed. Sending Radar Report...")
        radar_report = build_radar_report(signals[:5], strategy)
        notify_telegram(radar_report)

    log.info(f"Cycle done. {len(executed)} trades {'simulated' if dry_run else 'executed'}.")


def generate_account_report(strategy):
    """Generate a comprehensive account status report (Balance + Positions + P&L)."""
    log.info("Generating Account Report...")
    client = get_clob_client()
    balance = get_wallet_balance() or 0.0
    
    # 1. Active Positions (Token Balances)
    trades_log = load_trades()
    active_positions = []
    
    # Identify unique tokens we've traded
    token_metadata = {} # token_id -> {question, side}
    for t in trades_log.get("trades", []):
        if t.get("status") == "executed" and "token_id" in t:
            token_metadata[t["token_id"]] = {"q": t["question"], "side": t["side"]}

    # Check balance for each token we've ever held
    for tid, meta in token_metadata.items():
        try:
            # Fix: Use BalanceAllowanceParams object
            params = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=tid)
            b_info = client.get_balance_allowance(params)
            qty = float(b_info.get("balance", 0)) / 1e6
            if qty > 0.1: # Significant position
                active_positions.append(f"⚔️ {meta['side']} ({qty:.1f} sh) | {meta['q'][:50]}...")
        except:
            continue

    # 2. Recent Results (from trades.json)
    recent_history = []
    for t in trades_log.get("trades", [])[-5:]:
        status = "✅" if t.get("status") == "executed" else "❌"
        recent_history.append(f"{status} {t['event'][:40]}... (${t['amount_usd']:.2f})")

    # 3. Build Message
    lines = [
        "🐆 **PumaClaw Daily Report** 📊",
        f"📅 Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        f"💰 **Balance Disponible**: `${balance:.2f} USDC.e`",
        f"📈 **Historial de Inversión**: `${trades_log.get('stats', {}).get('total_wagered', 0):.2f}`",
        "",
        "⚔️ **Posiciones Activas (Holdings)**:",
    ]
    if active_positions:
        lines.extend(active_positions[:10])
    else:
        lines.append("   (No hay posiciones activas o tokens en cartera)")

    lines.extend([
        "",
        "🕒 **Últimos Movimientos Registrados**:",
    ])
    if recent_history:
        lines.extend(recent_history)
    else:
        lines.append("   (Sin actividad reciente)")

    lines.extend([
        "",
        f"🚀 **Meta $1M**: { (balance/1000000)*100 :.6f}% del colosal objetivo completado."
    ])
    
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="PumaClaw Autonomous Trader v2")
    parser.add_argument("--dry-run", action="store_true", help="Simulate")
    parser.add_argument("--once", action="store_true", help="One cycle only")
    parser.add_argument("--report", action="store_true", help="Generate and show account report")
    parser.add_argument("--notify", action="store_true", help="Send report to Telegram")
    args = parser.parse_args()

    strategy = load_strategy()
    
    if args.report:
        report_text = generate_account_report(strategy)
        print("\n" + report_text + "\n")
        if args.notify:
            notify_telegram(report_text)
        return

    dry_run = args.dry_run or strategy.get("dry_run", True)

    mode = "🧪 DRY RUN" if dry_run else "🔥 LIVE"
    log.info(f"PumaClaw Trader v2 starting — {mode}")
    log.info(f"Layers: L0(Gamma+Brave) → L1(LLM) → L2(Signal) → L3(Risk) → L4(Exec) → L5(Monitor)")

    interval = strategy["scan_interval_min"] * 60

    run_cycle(strategy, dry_run=dry_run)

    if args.once:
        return

    while _running:
        try:
            log.info(f"Sleeping {strategy['scan_interval_min']} min...")
            for _ in range(int(interval)):
                if not _running:
                    break
                time.sleep(1)
            if _running:
                strategy = load_strategy()
                dry_run = args.dry_run or strategy.get("dry_run", True)
                run_cycle(strategy, dry_run=dry_run)
        except Exception as e:
            log.error(f"FATAL ERROR in main loop: {e}")
            if strategy.get("self_healing", True):
                heal_bot(str(e))
            # Wait before retry to avoid infinite crash loops
            time.sleep(60)

    log.info("Trader stopped.")


if __name__ == "__main__":
    main()
