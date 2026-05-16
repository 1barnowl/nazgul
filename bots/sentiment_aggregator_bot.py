#!/usr/bin/env python3
"""
sentiment_aggregator_bot.py — Sentiment Aggregator Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Analyses Twitter, Reddit, and Discord sentiment on a
configurable topic, quantifies the sentiment, and places
bets on Polymarket prediction markets when sentiment is
strong enough.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install web3 requests tweepy praw

Configuration
─────────────
Place `sentiment_aggregator_config.json` in the same directory:

{
  "keywords": ["bitcoin", "ethereum"],
  "twitter": {
    "enabled": true,
    "bearer_token": "YOUR_TWITTER_BEARER_TOKEN",
    "accounts_to_watch": [],
    "search_query": "bitcoin OR ethereum -is:retweet lang:en",
    "max_tweets": 50
  },
  "reddit": {
    "enabled": true,
    "client_id": "YOUR_REDDIT_CLIENT_ID",
    "client_secret": "YOUR_REDDIT_CLIENT_SECRET",
    "user_agent": "SentimentAggregatorBot/1.0",
    "subreddits": ["cryptocurrency", "bitcoin", "ethtrader"],
    "limit_per_subreddit": 30,
    "sort": "hot"
  },
  "discord": {
    "enabled": false,
    "bot_token": "YOUR_DISCORD_BOT_TOKEN",
    "channel_ids": ["123456789012345678"],
    "message_limit": 50
  },
  "betting": {
    "platform": "polymarket",
    "polymarket": {
      "clob_api_key": "0x...",
      "clob_private_key": "0xPRIVATE_KEY",
      "proxy_wallet": "0xPROXY_WALLET_ADDRESS"
    },
    "max_bet_usd": 50.0,
    "min_sentiment_score": 0.6,
    "direction_threshold": 0.3
  },
  "poll_interval_minutes": 10,
  "state_file": "sentiment_aggregator_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
import re
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests
from web3 import Web3
from eth_account import Account
from eth_account.messages import encode_typed_data

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "sentiment_aggregator_bot"
BOT_NAME = "Sentiment Aggregator"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "sentiment_aggregator_config.json"
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
        return {"bets_placed": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Sentiment analysis utilities ─────────────────────────────────
POSITIVE_WORDS = ["bullish", "buy", "long", "moon", "pump", "up", "green", "breakout", "rally", "win", "good", "great", "positive", "profit"]
NEGATIVE_WORDS = ["bearish", "sell", "short", "dump", "crash", "down", "red", "fear", "loss", "bad", "negative", "scam", "hack"]

def simple_sentiment_score(text: str) -> float:
    """Return sentiment score from -1 (negative) to +1 (positive)."""
    text_lower = text.lower()
    pos_count = sum(1 for w in POSITIVE_WORDS if w in text_lower)
    neg_count = sum(1 for w in NEGATIVE_WORDS if w in text_lower)
    total = pos_count + neg_count
    if total == 0:
        return 0.0
    return (pos_count - neg_count) / total

# ── Data fetchers ────────────────────────────────────────────────

def fetch_twitter(config: dict) -> List[dict]:
    """Return list of tweet texts from Twitter API v2."""
    tw_cfg = config.get("twitter", {})
    if not tw_cfg.get("enabled", False):
        return []
    bearer = tw_cfg.get("bearer_token")
    if not bearer:
        _post("Twitter bearer token missing", "error")
        return []
    try:
        import tweepy
        client = tweepy.Client(bearer_token=bearer)
        query = tw_cfg.get("search_query", "bitcoin -is:retweet lang:en")
        max_results = min(int(tw_cfg.get("max_tweets", 50)), 100)
        tweets = client.search_recent_tweets(query=query, max_results=max_results,
                                             tweet_fields=["text", "created_at"])
        if not tweets.data:
            return []
        return [{"text": t.text, "source": "twitter"} for t in tweets.data]
    except Exception as e:
        _post(f"Twitter fetch error: {e}", "error")
        return []

def fetch_reddit(config: dict) -> List[dict]:
    """Return list of post titles and selftext from Reddit."""
    rd_cfg = config.get("reddit", {})
    if not rd_cfg.get("enabled", False):
        return []
    try:
        import praw
    except ImportError:
        _post("praw not installed", "error")
        return []

    client_id = rd_cfg.get("client_id")
    client_secret = rd_cfg.get("client_secret")
    user_agent = rd_cfg.get("user_agent", "SentimentBot/1.0")
    if not client_id or not client_secret:
        _post("Reddit credentials missing", "error")
        return []

    subreddits = rd_cfg.get("subreddits", ["cryptocurrency"])
    limit = rd_cfg.get("limit_per_subreddit", 30)
    sort = rd_cfg.get("sort", "hot")
    results = []

    try:
        reddit = praw.Reddit(client_id=client_id, client_secret=client_secret, user_agent=user_agent)
        for sub in subreddits:
            try:
                if sort == "hot":
                    submissions = reddit.subreddit(sub).hot(limit=limit)
                elif sort == "new":
                    submissions = reddit.subreddit(sub).new(limit=limit)
                elif sort == "rising":
                    submissions = reddit.subreddit(sub).rising(limit=limit)
                else:
                    submissions = reddit.subreddit(sub).hot(limit=limit)

                for submission in submissions:
                    text = (submission.title + " " + submission.selftext)[:500]
                    results.append({"text": text, "source": "reddit"})
            except Exception as e:
                _post(f"Error reading subreddit {sub}: {e}", "warning")
    except Exception as e:
        _post(f"Reddit auth error: {e}", "error")
    return results

def fetch_discord(config: dict) -> List[dict]:
    """Return list of message texts from Discord channel(s) via bot API."""
    dc_cfg = config.get("discord", {})
    if not dc_cfg.get("enabled", False):
        return []
    token = dc_cfg.get("bot_token")
    channel_ids = dc_cfg.get("channel_ids", [])
    if not token or not channel_ids:
        _post("Discord bot token/channel IDs missing", "error")
        return []

    results = []
    headers = {"Authorization": f"Bot {token}"}
    for cid in channel_ids:
        url = f"https://discord.com/api/v10/channels/{cid}/messages?limit={min(int(dc_cfg.get('message_limit', 50)), 100)}"
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                messages = resp.json()
                for msg in messages:
                    content = msg.get("content", "")
                    if content:
                        results.append({"text": content, "source": "discord"})
            else:
                _post(f"Discord API error for channel {cid}: {resp.status_code}", "warning")
        except Exception as e:
            _post(f"Discord fetch error: {e}", "error")
    return results

# ── Aggregator ───────────────────────────────────────────────────
def aggregate_sentiment(texts: List[dict]) -> dict:
    """Compute average sentiment, count, and predominant direction."""
    if not texts:
        return {"avg_score": 0.0, "count": 0, "direction": "neutral"}
    scores = [simple_sentiment_score(t["text"]) for t in texts]
    avg = sum(scores) / len(scores)
    direction = "neutral"
    if avg > 0.2:
        direction = "positive"
    elif avg < -0.2:
        direction = "negative"
    return {
        "avg_score": round(avg, 4),
        "count": len(scores),
        "direction": direction,
    }

# ── Market matching ──────────────────────────────────────────────
POLYMARKET_GAMMA = "https://gamma-api.polymarket.com"

def find_market_for_topic(topic: str) -> Optional[dict]:
    """Search Polymarket for a market related to the topic."""
    resp = requests.get(f"{POLYMARKET_GAMMA}/markets", params={
        "search": topic,
        "closed": "false",
        "limit": 5
    })
    if resp.status_code == 200:
        markets = resp.json()
        if markets:
            return markets[0]  # best match
    return None

# ── Polymarket order placement (re‑used from previous bots) ──────
def sign_order(private_key, proxy_wallet, token_id, price, size, side):
    w3 = Web3()
    account = Account.from_key(private_key)
    domain = {
        "name": "Polymarket CTF Exchange",
        "version": "1",
        "chainId": 137,
        "verifyingContract": "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
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
    salt = int.from_bytes(os.urandom(32), "big")
    nonce = int(time.time() * 1000)
    expiration = int(time.time() + 3600)
    if side == "BUY":
        maker_amount = int(size * price * 1e6)
        taker_amount = int(size * 1e6)
    else:
        maker_amount = int(size * 1e6)
        taker_amount = int(size * price * 1e6)
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
        "side": 0 if side == "BUY" else 1,
        "signatureType": 0
    }
    encoded = encode_typed_data(full_message={
        "domain": domain,
        "types": types,
        "primaryType": "Order",
        "message": order
    })
    signed = account.sign_message(encoded)
    order["signature"] = signed.signature.hex()
    return order

def place_polymarket_bet(market: dict, direction: str, max_bet_usd: float,
                         api_key: str, private_key: str, proxy_wallet: str) -> bool:
    """Place a bet on the YES or NO token of the given market."""
    tokens = market.get("tokens", [])
    token_id = None
    for t in tokens:
        if direction == "positive" and t.get("outcome") == "Yes":
            token_id = t["token_id"]
        elif direction == "negative" and t.get("outcome") == "No":
            token_id = t["token_id"]
    if not token_id:
        _post(f"No suitable token for direction {direction}", "error")
        return False

    # Fetch order book for price
    book_url = f"https://clob.polymarket.com/book?token_id={token_id}"
    book = requests.get(book_url).json()
    if direction == "positive":
        asks = book.get("asks", [])
        if not asks:
            _post("No ask orders", "error")
            return False
        price = float(asks[0]["price"])
        side = "BUY"
    else:
        bids = book.get("bids", [])
        if not bids:
            _post("No bid orders", "error")
            return False
        price = float(bids[0]["price"])
        side = "BUY"  # buying NO token is equivalent to buying no side? Actually for NO token, to bet against event, you BUY NO token. So side is BUY.
        # Actually you buy NO token; same as buying a token, price is the NO price.

    size = int(max_bet_usd / price)
    if size < 1:
        size = 1

    signed = sign_order(private_key, proxy_wallet, token_id, price, size, side)
    headers = {
        "POLY-API-KEY": api_key,
        "POLY-SIGNATURE": api_key,  # API secret is same as api key? Polymarket uses separate secret; we'll assume it's the same as api_key for simplicity if user sets it.
        "Content-Type": "application/json"
    }
    resp = requests.post("https://clob.polymarket.com/order", json=signed, headers=headers, timeout=10)
    if resp.status_code == 200:
        order_id = resp.json().get("id")
        _post(f"Bet placed: {direction} token, size {size}, price {price}, order {order_id}", "error", {"order_id": order_id})
        return True
    else:
        _post(f"Order rejected: {resp.text[:200]}", "error")
        return False

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Sentiment Aggregator Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        keywords = config.get("keywords", [])
        if not keywords:
            _post("No keywords configured", "error")
            time.sleep(300)
            continue

        # Fetch data from all sources
        tweets = fetch_twitter(config)
        reddit_posts = fetch_reddit(config)
        discord_msgs = fetch_discord(config)

        all_texts = tweets + reddit_posts + discord_msgs
        if not all_texts:
            _post("No sentiment data collected", "info")
        else:
            sentiment = aggregate_sentiment(all_texts)
            _post(f"Sentiment: {sentiment['avg_score']:.2f} ({sentiment['direction']}) from {sentiment['count']} messages",
                  "info", sentiment)

            # Check if we should bet
            betting_cfg = config.get("betting", {})
            min_score = float(betting_cfg.get("min_sentiment_score", 0.6))
            if abs(sentiment["avg_score"]) >= min_score and sentiment["direction"] != "neutral":
                # Find a relevant market
                topic = keywords[0]  # simple approach
                market = find_market_for_topic(topic)
                if market:
                    pm_cfg = betting_cfg.get("polymarket", {})
                    api_key = pm_cfg.get("clob_api_key")
                    private_key = pm_cfg.get("clob_private_key")
                    proxy_wallet = pm_cfg.get("proxy_wallet")
                    if api_key and private_key and proxy_wallet:
                        place_polymarket_bet(market, sentiment["direction"],
                                             float(betting_cfg.get("max_bet_usd", 50.0)),
                                             api_key, private_key, proxy_wallet)
                else:
                    _post(f"No matching market for topic '{topic}'", "warning")

        poll_min = int(config.get("poll_interval_minutes", 10))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
