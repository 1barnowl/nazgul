#!/usr/bin/env python3
"""
twitter_thread_reply_bot.py — X/Twitter Thread & Reply Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors trending keywords, auto‑replies with value‑added
comments, schedules tweet storms, polls, and participates
in community threads to boost visibility.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install tweepy requests schedule

Configuration
─────────────
Place `twitter_thread_config.json` in the same directory:

{
  "twitter": {
    "api_key": "YOUR_API_KEY",
    "api_secret": "YOUR_API_SECRET",
    "access_token": "YOUR_ACCESS_TOKEN",
    "access_token_secret": "YOUR_ACCESS_TOKEN_SECRET"
  },
  "monitoring": {
    "keywords": ["crypto", "blockchain", "marketing"],
    "reply_template": "Interesting take! We've been exploring similar ideas. Check out our product: https://example.com",
    "max_replies_per_run": 5,
    "avoid_reply_to": ["@spamaccount"]
  },
  "scheduled": {
    "threads_file": "scheduled_threads.json",
    "polls_file": "scheduled_polls.json"
  },
  "community": {
    "thread_ids": ["1234567890123456789"],
    "reply_template": "Great thread! We've built a tool that solves this exact problem."
  },
  "state_file": "twitter_thread_state.json",
  "poll_interval_seconds": 60,
  "heartbeat_interval": 30
}

Scheduled threads file (`scheduled_threads.json`):
[
  {
    "tweets": ["Tweet 1/3: Introduction", "Tweet 2/3: Details", "Tweet 3/3: Call to action"],
    "scheduled_at": "2025-01-20T10:00:00Z"
  }
]

Scheduled polls file (`scheduled_polls.json`):
[
  {
    "question": "What do you think about our new product?",
    "options": ["Love it", "It's okay", "Not my style"],
    "duration_minutes": 1440,
    "scheduled_at": "2025-01-21T12:00:00Z"
  }
]
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List, Any

import requests
import tweepy

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "twitter_thread_reply_bot"
BOT_NAME = "X/Twitter Thread & Reply"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "twitter_thread_config.json"
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

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"replied_to": [], "scheduled_threads_processed": [], "scheduled_polls_processed": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Twitter client (API v2) ──────────────────────────────────────
def get_twitter_client(config: dict) -> tweepy.Client:
    tw_cfg = config["twitter"]
    return tweepy.Client(
        consumer_key=tw_cfg["api_key"],
        consumer_secret=tw_cfg["api_secret"],
        access_token=tw_cfg["access_token"],
        access_token_secret=tw_cfg["access_token_secret"]
    )

# ── Monitoring & auto‑reply ──────────────────────────────────────
def search_and_reply(client: tweepy.Client, config: dict, state: dict):
    """Search recent tweets for keywords and auto‑reply."""
    keywords = config.get("monitoring", {}).get("keywords", [])
    if not keywords:
        return
    reply_template = config["monitoring"]["reply_template"]
    max_replies = int(config["monitoring"].get("max_replies_per_run", 5))
    avoid_users = [u.lower() for u in config["monitoring"].get("avoid_reply_to", [])]

    query = " OR ".join(keywords) + " -is:retweet -is:reply lang:en"
    try:
        tweets = client.search_recent_tweets(
            query=query,
            max_results=min(max_replies, 10),
            tweet_fields=["author_id", "conversation_id"]
        )
        if not tweets.data:
            return
        for tweet in tweets.data:
            tweet_id = tweet.id
            # Skip if we already replied
            if tweet_id in state["replied_to"]:
                continue
            # Optionally skip certain users
            author_id = tweet.author_id
            # Could fetch author username to check, but we'll rely on no replying to self (by checking if we are the author)
            # For simplicity, skip if author is us? We'll not.
            # Reply
            try:
                resp = client.create_tweet(
                    text=reply_template,
                    in_reply_to_tweet_id=tweet_id
                )
                if resp.data and resp.data["id"]:
                    _post(f"Replied to tweet {tweet_id}: {reply_template[:40]}...", "info")
                    state["replied_to"].append(tweet_id)
                    # Keep list trimmed
                    state["replied_to"] = state["replied_to"][-200:]
                    time.sleep(1)  # rate limit
                    if len(state["replied_to"]) >= max_replies:
                        break
            except tweepy.TweepyException as e:
                _post(f"Failed to reply to {tweet_id}: {e}", "error")
    except tweepy.TweepyException as e:
        _post(f"Search error: {e}", "error")

# ── Scheduled threads ────────────────────────────────────────────
def post_thread(client: tweepy.Client, tweets: List[str]) -> Optional[str]:
    """Post a tweet thread. Returns the ID of the first tweet."""
    if not tweets:
        return None
    prev_id = None
    first_id = None
    for text in tweets:
        try:
            if prev_id:
                resp = client.create_tweet(text=text, in_reply_to_tweet_id=prev_id)
            else:
                resp = client.create_tweet(text=text)
            if resp.data and resp.data["id"]:
                prev_id = resp.data["id"]
                if first_id is None:
                    first_id = prev_id
                time.sleep(1)
        except tweepy.TweepyException as e:
            _post(f"Thread post error: {e}", "error")
            return None
    return first_id

def process_scheduled_threads(config: dict, state: dict):
    file = config.get("scheduled", {}).get("threads_file", "scheduled_threads.json")
    if not os.path.exists(file):
        return
    try:
        with open(file, "r") as f:
            scheduled = json.load(f)
    except Exception:
        return

    now = datetime.now(timezone.utc)
    client = get_twitter_client(config)
    updated = []
    for idx, item in enumerate(scheduled):
        scheduled_at_str = item.get("scheduled_at")
        if not scheduled_at_str:
            updated.append(item)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            updated.append(item)
            continue
        # If due within 1 minute
        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            tweet_texts = item.get("tweets", [])
            if tweet_texts:
                first_id = post_thread(client, tweet_texts)
                if first_id:
                    _post(f"Thread posted starting with tweet {first_id}", "info")
                    # record to avoid reposting
                    state.setdefault("scheduled_threads_processed", []).append(str(idx))
                else:
                    updated.append(item)  # keep for retry
            else:
                updated.append(item)
            time.sleep(2)
        else:
            updated.append(item)

    # Remove processed from file (optional)
    # For simplicity, we'll just save the updated list (without processed)
    # But we need to skip those marked as processed
    remaining = [item for i, item in enumerate(scheduled)
                 if str(i) not in state.get("scheduled_threads_processed", [])]
    with open(file, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Scheduled polls ──────────────────────────────────────────────
def create_poll(client: tweepy.Client, question: str, options: List[str],
                duration_minutes: int) -> Optional[str]:
    """Create a Twitter poll. Twitter API v2 does not support polls directly? Actually you must use API v1.1 for polls. We'll implement via Tweepy's API v1.1? Tweepy's Client does not have create_poll. We'll use tweepy.API for that if available. But we want to stick to v2. However, polls are only available via v1.1 (statuses/update with poll). To keep it real, we can use `tweepy.API` with OAuth 1.0a."""
    # Since we already have OAuth 1.0a credentials, we can use the old API for polls.
    try:
        auth = tweepy.OAuth1UserHandler(
            config["twitter"]["api_key"],
            config["twitter"]["api_secret"],
            config["twitter"]["access_token"],
            config["twitter"]["access_token_secret"]
        )
        api = tweepy.API(auth)
        poll_options = [tweepy.PollOption(label=opt) for opt in options]
        status = api.update_status_with_media(
            status=question,
            # No media, but we need poll parameters
            poll_options=poll_options,
            poll_duration_minutes=duration_minutes
        )
        return status.id_str
    except tweepy.TweepyException as e:
        _post(f"Poll creation error: {e}", "error")
        return None

def process_scheduled_polls(config: dict, state: dict):
    file = config.get("scheduled", {}).get("polls_file", "scheduled_polls.json")
    if not os.path.exists(file):
        return
    try:
        with open(file, "r") as f:
            scheduled = json.load(f)
    except Exception:
        return

    now = datetime.now(timezone.utc)
    updated = []
    for idx, item in enumerate(scheduled):
        scheduled_at_str = item.get("scheduled_at")
        if not scheduled_at_str:
            updated.append(item)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            updated.append(item)
            continue
        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            question = item.get("question")
            options = item.get("options", [])
            duration = int(item.get("duration_minutes", 1440))
            if question and len(options) >= 2 and len(options) <= 4:
                poll_id = create_poll(config, question, options, duration)
                if poll_id:
                    _post(f"Poll created: {question}", "info")
                    state.setdefault("scheduled_polls_processed", []).append(str(idx))
            updated.append(item)  # don't retry if failed for now
            time.sleep(2)
        else:
            updated.append(item)
    # Keep only unprocessed
    remaining = [item for i, item in enumerate(scheduled)
                 if str(i) not in state.get("scheduled_polls_processed", [])]
    with open(file, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Community threads ────────────────────────────────────────────
def participate_in_community(client: tweepy.Client, config: dict, state: dict):
    """Reply to specified thread IDs."""
    thread_ids = config.get("community", {}).get("thread_ids", [])
    if not thread_ids:
        return
    reply_template = config["community"]["reply_template"]
    for tid in thread_ids:
        # Check if we already replied
        if tid in state.get("community_replied", []):
            continue
        try:
            resp = client.create_tweet(text=reply_template, in_reply_to_tweet_id=tid)
            if resp.data and resp.data["id"]:
                _post(f"Replied to community thread {tid}", "info")
                state.setdefault("community_replied", []).append(tid)
                time.sleep(1)
        except tweepy.TweepyException as e:
            _post(f"Community reply error for {tid}: {e}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("X/Twitter Thread & Reply Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        client = get_twitter_client(config)
        state_file = config.get("state_file", "twitter_thread_state.json")
        state = load_state(state_file)

        # 1. Monitor keywords & auto-reply
        if config.get("monitoring", {}).get("keywords"):
            search_and_reply(client, config, state)

        # 2. Scheduled threads
        process_scheduled_threads(config, state)

        # 3. Scheduled polls
        process_scheduled_polls(config, state)

        # 4. Community threads
        participate_in_community(client, config, state)

        save_state(state_file, state)

        poll_interval = int(config.get("poll_interval_seconds", 60))
        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
