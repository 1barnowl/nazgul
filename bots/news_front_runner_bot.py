#!/usr/bin/env python3
"""
news_front_runner_bot.py — News Event Front‑Runner Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Watches real‑time news headlines via NewsAPI and Twitter,
scores them for impact, matches to open Polymarket binary
events, and places a bet within seconds – before the crowd
moves the odds.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests tweepy python-dateutil

Configuration
─────────────
Place `news_frontrunner_config.json` in the same directory:

{
  "news": {
    "newsapi_key": "YOUR_NEWSAPI_KEY",
    "keywords": ["bitcoin", "ethereum", "sec", "fed", "inflation", "gdp", "unemployment", "crypto", "lawsuit", "regulation"],
    "fetch_interval_seconds": 30
  },
  "twitter": {
    "enabled": false,
    "bearer_token": "YOUR_TWITTER_BEARER_TOKEN",
    "accounts_to_watch": ["Reuters", "Bloomberg", "CoinDesk"],
    "fetch_interval_seconds": 60
  },
  "prediction_market": {
    "platform": "polymarket",                 // only polymarket for now
    "clob_api_key": "0x...",                  // your CLOB API key (wallet address)
    "clob_private_key": "0xPRIVATE_KEY",      // private key of the proxy wallet
    "proxy_wallet": "0xPROXY_WALLET_ADDRESS",
    "gamma_api_key": "PK...",                 // optional, for market search
    "gamma_api_secret": "sk..."
  },
  "betting": {
    "max_bet_usd": 50.0,
    "min_confidence_score": 0.7,              // 0-1, required news impact score to bet
    "default_direction": "buy_yes"            // "buy_yes" or "buy_no"
  },
  "state_file": "news_frontrunner_state.json",
  "heartbeat_interval": 30,
  "poll_interval_seconds": 30
}
"""

import json
import os
import time
import hashlib
import hmac
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data
from dateutil import parser as dateparser

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "news_front_runner_bot"
BOT_NAME = "News Event Front‑Runner"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "news_frontrunner_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID,
            "bot_name": BOT_NAME,
            "summary": summary,
            "level": level,
            "payload": payload or {},
        }, timeout=5)
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME,
            "status": "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── State management ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"seen_articles": [], "bet_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── News scoring ────────────────────────────────────────────────
IMPACT_WORDS = {
    "high": ["sec", "lawsuit", "ban", "hacked", "crash", "emergency", "war", "default", "collapse",
             "approves", "approval", "greenlight", "breaking", "exclusive"],
    "medium": ["announces", "raise", "cut", "inflation", "gdp", "unemployment", "partnership", "launch",
               "update", "upgrade", "downgrade"],
}

def score_news_impact(title: str, description: str = "") -> float:
    """Return a score 0‑1 based on presence of high/medium impact words."""
    text = (title + " " + description).lower()
    score = 0.0
    for word in IMPACT_WORDS["high"]:
        if word in text:
            score += 0.25
    for word in IMPACT_WORDS["medium"]:
        if word in text:
            score += 0.15
    # Bonus for very short, all‑caps titles (typical of breaking news)
    if title.isupper() and len(title.split()) < 6:
        score += 0.2
    return min(score, 1.0)

# ── News fetching (NewsAPI) ──────────────────────────────────────
NEWSAPI_URL = "https://newsapi.org/v2/top-headlines"

def fetch_newsapi_articles(api_key: str, keywords: list) -> List[dict]:
    """Fetch headlines matching keywords from NewsAPI."""
    all_articles = []
    for kw in keywords:
        try:
            resp = requests.get(NEWSAPI_URL, params={
                "apiKey": api_key,
                "q": kw,
                "language": "en",
                "pageSize": 20,
            }, timeout=10)
            if resp.status_code == 200:
                articles = resp.json().get("articles", [])
                all_articles.extend(articles)
            else:
                _post(f"NewsAPI error for '{kw}': {resp.status_code}", "warning")
        except Exception as e:
            _post(f"NewsAPI fetch error: {e}", "error")
        time.sleep(1)  # avoid rate limits (1000 req/day free tier)
    # Deduplicate by URL
    unique = []
    seen_urls = set()
    for art in all_articles:
        url = art.get("url", "")
        if url not in seen_urls:
            seen_urls.add(url)
            unique.append(art)
    return unique

# ── Twitter fetching (optional) ──────────────────────────────────
def fetch_twitter_headlines(config: dict) -> List[dict]:
    """Fetch recent tweets from watched accounts."""
    tw_cfg = config.get("twitter", {})
    if not tw_cfg.get("enabled"):
        return []
    bearer_token = tw_cfg.get("bearer_token")
    if not bearer_token:
        return []
    try:
        import tweepy
    except ImportError:
        _post("tweepy not installed; skip Twitter", "error")
        return []

    client = tweepy.Client(bearer_token=bearer_token)
    accounts = tw_cfg.get("accounts_to_watch", [])
    articles = []
    for account in accounts:
        try:
            user = client.get_user(username=account)
            if not user.data:
                continue
            tweets = client.get_users_tweets(id=user.data.id, max_results=5,
                                             tweet_fields=["created_at", "text"])
            if tweets.data:
                for tweet in tweets.data:
                    articles.append({
                        "title": tweet.text[:100],
                        "description": tweet.text,
                        "url": f"https://twitter.com/{account}/status/{tweet.id}",
                        "source": f"twitter/{account}",
                        "publishedAt": tweet.created_at.isoformat() if tweet.created_at else ""
                    })
        except Exception as e:
            _post(f"Twitter error for {account}: {e}", "warning")
    return articles

# ── Polymarket matching ─────────────────────────────────────────
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

def search_polymarket_markets(query: str) -> List[dict]:
    """Search Polymarket for markets matching the query."""
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets", params={
        "closed": "false",
        "limit": 10,
        "search": query
    })
    if resp.status_code == 200:
        return resp.json()
    return []

def find_matching_market(news_title: str) -> Optional[dict]:
    """Find the best matching Polymarket market for a news headline."""
    # Simplify: extract main subject from title (crude NLP)
    words = news_title.lower().split()
    # Try the whole title as search
    markets = search_polymarket_markets(news_title[:100])
    if markets:
        return markets[0]  # assume first is best match
    # Try key noun phrases (e.g., "SEC approves Bitcoin ETF")
    # For simplicity, use title as query; fail if no match.
    return None

# ── Polymarket order placement (CLOB) ────────────────────────────
POLYMARKET_CLOB = "https://clob.polymarket.com"

def sign_polymarket_order(private_key: str, proxy_wallet: str,
                          token_id: str, price: float, size: int, side: str) -> dict:
    """
    Create a signed EIP‑712 order for the Polymarket CLOB.
    Price: in decimal dollars (e.g., 0.55)
    Size: number of shares (each share is $1 collateral)
    side: "BUY" or "SELL"
    Returns signed order dict ready for POST to /order.
    """
    w3 = Web3()
    account = Account.from_key(private_key)
    # The CLOB expects price as string decimal, size as string integer.
    # The EIP‑712 typed data structure is defined by Polymarket.
    domain = {
        "name": "Polymarket CTF Exchange",
        "version": "1",
        "chainId": 137,     # Polygon mainnet
        "verifyingContract": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"  # CTF Exchange address
    }
    types = {
        "Order": [
            {"name": "salt", "type": "uint256"},
            {"name": "maker", "type": "address"},
            {"name": "signer", "type": "address"},
            {"name": "taker", "type": "address"},
            {"name": "tokenId", "type": "uint256"},
            {"name": "makerAmount", "type": "uint256"},
            {"name": "takerAmount", "type": "uint256"},
            {"name": "expiration", "type": "uint256"},
            {"name": "nonce", "type": "uint256"},
            {"name": "feeRateBps", "type": "uint256"},
            {"name": "side", "type": "uint8"},
            {"name": "signatureType", "type": "uint8"}
        ]
    }
    # Generate a unique salt
    salt = int.from_bytes(os.urandom(32), byteorder='big')
    nonce = int(time.time())  # simplistic nonce, better use CLOB nonce manager
    expiration = int(time.time() + 3600)  # 1 hour
    # Maker/taker amounts: for BUY, maker wants to receive token shares, so makerAmount = size * 1e6 (if token decimals = 6 for collateral)
    # Actually the collateral token (USDC) has 6 decimals on Polygon. Each share is $1, so price * size = amount in USDC. The tokenId represents the outcome token.
    maker_amount = int(size * 1e6) if side == "BUY" else int(size * price * 1e6)
    taker_amount = int(size * price * 1e6) if side == "BUY" else int(size * 1e6)

    order = {
        "salt": salt,
        "maker": proxy_wallet,
        "signer": proxy_wallet,
        "taker": "0x0000000000000000000000000000000000000000",
        "tokenId": int(token_id),
        "makerAmount": maker_amount,
        "takerAmount": taker_amount,
        "expiration": expiration,
        "nonce": nonce,
        "feeRateBps": 0,
        "side": 0 if side == "BUY" else 1,   # 0 = BUY, 1 = SELL
        "signatureType": 0  # EIP‑712 typed data
    }
    # Encode and sign
    encoded_data = encode_typed_data(full_message={"domain": domain, "types": types, "primaryType": "Order", "message": order})
    signed = account.sign_message(encoded_data)
    order["signature"] = signed.signature.hex()
    return order

def polymarket_submit_order(signed_order: dict, api_key: str, api_secret: str) -> Optional[str]:
    """POST the signed order to Polymarket CLOB; returns order ID."""
    headers = {
        "POLY-API-KEY": api_key,
        "POLY-SIGNATURE": api_secret,
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(f"{POLYMARKET_CLOB}/order", json=signed_order, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return data.get("id")
        else:
            _post(f"Polymarket order rejected: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Order submission error: {e}", "error")
        return None

def execute_bet(market: dict, direction: str, max_bet_usd: float,
                clob_api_key: str, clob_private_key: str, proxy_wallet: str,
                gamma_api_key: str, gamma_api_secret: str) -> Optional[str]:
    """
    Place a bet on the given market.
    direction: "buy_yes", "buy_no", "sell_yes", "sell_no"
    Returns order ID or None.
    """
    # Determine token ID for YES or NO
    tokens = market.get("tokens", [])
    token_id = None
    for t in tokens:
        if direction in ("buy_yes", "sell_yes") and t.get("outcome") == "Yes":
            token_id = t["token_id"]
        elif direction in ("buy_no", "sell_no") and t.get("outcome") == "No":
            token_id = t["token_id"]
    if not token_id:
        _post("Cannot find appropriate token", "error")
        return None

    # Determine side (BUY or SELL) from direction
    if direction.startswith("buy"):
        side = "BUY"
    else:
        side = "SELL"

    # Price: use current best ask (for BUY) or best bid (for SELL)
    # We'll fetch the order book quickly
    book = requests.get(f"{POLYMARKET_CLOB}/book?token_id={token_id}").json()
    if side == "BUY":
        asks = book.get("asks", [])
        if not asks:
            _post("No asks in order book", "error")
            return None
        price = float(asks[0]["price"])
    else:
        bids = book.get("bids", [])
        if not bids:
            _post("No bids in order book", "error")
            return None
        price = float(bids[0]["price"])

    # Calculate size: max_bet_usd / price = number of shares (each share is $1)
    size = int(max_bet_usd / price)
    if size < 1:
        size = 1

    # Sign order
    signed_order = sign_polymarket_order(clob_private_key, proxy_wallet, token_id, price, size, side)
    order_id = polymarket_submit_order(signed_order, clob_api_key, gamma_api_secret)
    return order_id

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("News Event Front‑Runner Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        news_cfg = config.get("news", {})
        twitter_cfg = config.get("twitter", {})
        pm_cfg = config.get("prediction_market", {})
        bet_cfg = config.get("betting", {})
        state_file = config.get("state_file", "news_frontrunner_state.json")

        newsapi_key = news_cfg.get("newsapi_key")
        if not newsapi_key:
            _post("NewsAPI key missing", "error")
            time.sleep(300)
            continue

        max_bet_usd = float(bet_cfg.get("max_bet_usd", 50.0))
        min_score = float(bet_cfg.get("min_confidence_score", 0.7))
        default_direction = bet_cfg.get("default_direction", "buy_yes")

        # Load state
        state = load_state(state_file)
        seen_articles = set(state.get("seen_articles", []))

        # Fetch news
        keywords = news_cfg.get("keywords", [])
        articles = fetch_newsapi_articles(newsapi_key, keywords)
        # Fetch Twitter (optional)
        if twitter_cfg.get("enabled"):
            articles.extend(fetch_twitter_headlines(config))

        new_articles = []
        for art in articles:
            url = art.get("url", "")
            if url and url in seen_articles:
                continue
            new_articles.append(art)
            if url:
                seen_articles.add(url)

        # Process each new article
        for art in new_articles:
            title = art.get("title", "")
            desc = art.get("description", "")
            score = score_news_impact(title, desc)
            if score < min_score:
                continue
            # Find matching prediction market
            market = find_matching_market(title)
            if not market:
                # No matching market, maybe try broader search
                continue

            # Determine bet direction based on sentiment (simplistic)
            direction = default_direction
            # If negative keywords present, buy NO (if default buy_yes)
            negative_words = ["crash", "ban", "lawsuit", "default", "hacked"]
            if any(w in (title+" "+desc).lower() for w in negative_words):
                if default_direction == "buy_yes":
                    direction = "buy_no"
                elif default_direction == "buy_no":
                    direction = "buy_yes"

            # Check if we already bet on this market for similar news? not implemented
            _post(f"High‑impact news (score {score:.2f}): '{title[:80]}' → "
                  f"market {market.get('question')} bet {direction}",
                  "warning", {"title": title, "market": market, "direction": direction})

            # Place the bet (real funds)
            pm_clob_key = pm_cfg.get("clob_api_key")
            pm_clob_private = pm_cfg.get("clob_private_key")
            proxy_wallet = pm_cfg.get("proxy_wallet")
            gamma_key = pm_cfg.get("gamma_api_key", "")
            gamma_secret = pm_cfg.get("gamma_api_secret", "")

            if pm_clob_private and pm_clob_key and proxy_wallet:
                order_id = execute_bet(market, direction, max_bet_usd,
                                       pm_clob_key, pm_clob_private, proxy_wallet,
                                       gamma_key, gamma_secret)
                if order_id:
                    _post(f"Bet placed: order {order_id}", "error", {"order_id": order_id})
                    state.setdefault("bet_ids", []).append(order_id)
            else:
                _post("Missing CLOB credentials; skipping actual bet", "warning")

        # Update state (keep last 2000 seen URLs)
        state["seen_articles"] = list(seen_articles)[-2000:]
        save_state(state_file, state)

        poll_sec = int(config.get("poll_interval_seconds", 30))
        _heartbeat()
        time.sleep(poll_sec)

if __name__ == "__main__":
    main()
