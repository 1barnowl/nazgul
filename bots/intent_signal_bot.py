#!/usr/bin/env python3
"""
intent_signal_bot.py — Intent Signal Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors Reddit, X (Twitter), and LinkedIn for intent
phrases like "looking for recommendations", "anyone
use X/Y/Z?", "best tool for...", etc. Detects and
scores commercial intent signals, reporting them to
the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests praw twikit

Configuration
─────────────
Place `intent_signal_config.json` in the same directory:

{
  "platforms": {
    "reddit": {
      "enabled": true,
      "method": "public_json",
      "client_id": null,
      "client_secret": null,
      "user_agent": "IntentSignalBot/1.0",
      "subreddits": ["all"],
      "limit_per_query": 25,
      "sort": "new",
      "time_range": "week"
    },
    "twitter": {
      "enabled": false,
      "method": "twikit",
      "username": null,
      "email": null,
      "password": null,
      "cookies_file": "twitter_cookies.json",
      "bearer_token": null,
      "limit_per_query": 20
    },
    "linkedin": {
      "enabled": false,
      "method": "rapidapi",
      "rapidapi_key": null,
      "rapidapi_host": "comprehensive-linkedin-platform.p.rapidapi.com",
      "limit_per_query": 20
    }
  },
  "intent_phrases": [
    "looking for recommendations",
    "anyone use",
    "best tool for",
    "what do you use for",
    "alternatives to",
    "recommend me",
    "should I buy",
    "worth the money",
    "is it worth it",
    "any experience with",
    "thinking of getting",
    "trying to decide between",
    "need help choosing",
    "which is better",
    "how does X compare to"
  ],
  "intent_scoring": {
    "strong_indicators": [
      "recommend", "best", "should I buy", "worth",
      "trying to decide", "need help choosing"
    ],
    "weak_indicators": [
      "anyone use", "what do you", "how does",
      "any experience", "looking for"
    ],
    "min_score": 1,
    "alert_threshold": 3
  },
  "poll_interval_minutes": 30,
  "state_file": "intent_signal_state.json",
  "user_agent": "IntentSignalBot/1.0"
}
"""

import json
import os
import re
import time
import asyncio
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict, List

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "intent_signal_bot"
BOT_NAME = "Intent Signal"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "intent_signal_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id":   BOT_ID,
            "bot_name": BOT_NAME,
            "summary":  summary,
            "level":    level,
            "payload":  payload or {},
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
            "status":   "online",
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
        return {"last_ids": {}, "last_timestamps": {}}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Intent scoring ───────────────────────────────────────────────
def score_intent(text: str, config: dict) -> int:
    """Score text for commercial intent strength. Higher = stronger signal."""
    text_lower = text.lower()
    scoring = config.get("intent_scoring", {})
    strong = scoring.get("strong_indicators", [])
    weak = scoring.get("weak_indicators", [])
    score = 0
    for kw in strong:
        if kw.lower() in text_lower:
            score += 3
    for kw in weak:
        if kw.lower() in text_lower:
            score += 1
    # Bonus: question marks indicate asking for advice
    if "?" in text:
        score += 1
    return score

def matches_intent_phrases(text: str, phrases: List[str]) -> bool:
    """Check if text contains any intent phrase."""
    text_lower = text.lower()
    return any(phrase.lower() in text_lower for phrase in phrases)

# ── Reddit fetcher ───────────────────────────────────────────────
def fetch_reddit(config: dict, state: dict) -> List[dict]:
    """Search Reddit for intent signals using public JSON or PRAW."""
    plat_cfg = config.get("platforms", {}).get("reddit", {})
    if not plat_cfg.get("enabled"):
        return []

    method = plat_cfg.get("method", "public_json")
    if method == "praw":
        return _fetch_reddit_praw(plat_cfg, config, state)
    else:
        return _fetch_reddit_json(plat_cfg, config, state)

def _fetch_reddit_json(plat_cfg: dict, config: dict, state: dict) -> List[dict]:
    """Use Reddit's public JSON endpoints (no API key needed)."""
    user_agent = plat_cfg.get("user_agent") or config.get("user_agent", "IntentSignalBot/1.0")
    phrases = config.get("intent_phrases", [])
    subreddits = plat_cfg.get("subreddits", ["all"])
    limit = plat_cfg.get("limit_per_query", 25)
    sort = plat_cfg.get("sort", "new")
    time_range = plat_cfg.get("time_range", "week")
    headers = {"User-Agent": user_agent}
    seen_ids = set(state.get("last_ids", {}).get("reddit", []))
    results = []

    for subreddit in subreddits:
        for phrase in phrases[:5]:  # Limit to top 5 phrases to avoid rate limiting
            query = phrase.replace(" ", "+")
            if subreddit.lower() == "all":
                url = f"https://www.reddit.com/search.json?q={query}&limit={limit}&sort={sort}&t={time_range}&raw_json=1"
            else:
                url = f"https://www.reddit.com/r/{subreddit}/search.json?q={query}&restrict_sr=on&limit={limit}&sort={sort}&t={time_range}&raw_json=1"

            try:
                resp = requests.get(url, headers=headers, timeout=15)
                if resp.status_code == 429:
                    time.sleep(2)
                    continue
                if resp.status_code != 200:
                    continue
                data = resp.json()
                posts = data.get("data", {}).get("children", [])
                for child in posts:
                    post = child.get("data", {})
                    post_id = post.get("id", "")
                    if post_id in seen_ids:
                        continue
                    seen_ids.add(post_id)
                    text = (post.get("title", "") + " " + post.get("selftext", ""))
                    if matches_intent_phrases(text, phrases):
                        score = score_intent(text, config)
                        results.append({
                            "platform": "reddit",
                            "post_id": post_id,
                            "title": post.get("title", ""),
                            "body": post.get("selftext", "")[:500],
                            "url": f"https://reddit.com{post.get('permalink', '')}",
                            "author": str(post.get("author", "")),
                            "subreddit": str(post.get("subreddit", "")),
                            "score": score,
                            "ups": post.get("score", 0),
                            "num_comments": post.get("num_comments", 0),
                            "created_utc": post.get("created_utc", 0)
                        })
                time.sleep(1.1)  # Rate limit: ~1 req/sec
            except Exception as e:
                _post(f"Reddit fetch error: {e}", "warning")
                continue

    # Keep only last 2000 seen IDs
    state.setdefault("last_ids", {})["reddit"] = list(seen_ids)[-2000:]
    return results

def _fetch_reddit_praw(plat_cfg: dict, config: dict, state: dict) -> List[dict]:
    """Use PRAW with OAuth credentials for Reddit."""
    try:
        import praw
    except ImportError:
        _post("praw not installed. Install with: pip install praw", "error")
        return []

    client_id = plat_cfg.get("client_id")
    client_secret = plat_cfg.get("client_secret")
    user_agent = plat_cfg.get("user_agent") or config.get("user_agent", "IntentSignalBot/1.0")
    if not client_id or not client_secret:
        _post("Reddit OAuth credentials not configured", "warning")
        return []

    phrases = config.get("intent_phrases", [])
    subreddits = plat_cfg.get("subreddits", ["all"])
    limit = plat_cfg.get("limit_per_query", 25)
    time_filter = plat_cfg.get("time_range", "week")
    seen_ids = set(state.get("last_ids", {}).get("reddit", []))
    results = []

    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent=user_agent
        )
    except Exception as e:
        _post(f"PRAW authentication error: {e}", "error")
        return []

    for phrase in phrases:
        try:
            if subreddits[0].lower() == "all":
                submissions = reddit.subreddit("all").search(phrase, sort="new", time_filter=time_filter, limit=limit)
            else:
                subreddit_str = "+".join(subreddits)
                submissions = reddit.subreddit(subreddit_str).search(phrase, sort="new", time_filter=time_filter, limit=limit)

            for submission in submissions:
                if submission.id in seen_ids:
                    continue
                seen_ids.add(submission.id)
                text = (submission.title + " " + submission.selftext)
                if matches_intent_phrases(text, phrases):
                    score = score_intent(text, config)
                    results.append({
                        "platform": "reddit",
                        "post_id": submission.id,
                        "title": submission.title,
                        "body": submission.selftext[:500],
                        "url": f"https://reddit.com{submission.permalink}",
                        "author": str(submission.author),
                        "subreddit": str(submission.subreddit),
                        "score": score,
                        "ups": submission.score,
                        "num_comments": submission.num_comments,
                        "created_utc": submission.created_utc
                    })
            time.sleep(2)
        except Exception as e:
            _post(f"Reddit PRAW search error: {e}", "warning")
            continue

    state.setdefault("last_ids", {})["reddit"] = list(seen_ids)[-2000:]
    return results

# ── Twitter/X fetcher ────────────────────────────────────────────
def fetch_twitter(config: dict, state: dict) -> List[dict]:
    """Search X/Twitter for intent signals using Twikit or official API."""
    plat_cfg = config.get("platforms", {}).get("twitter", {})
    if not plat_cfg.get("enabled"):
        return []

    method = plat_cfg.get("method", "twikit")
    if method == "tweepy":
        return _fetch_twitter_tweepy(plat_cfg, config, state)
    else:
        return _fetch_twitter_twikit(plat_cfg, config, state)

def _fetch_twitter_twikit(plat_cfg: dict, config: dict, state: dict) -> List[dict]:
    """Use Twikit (free, internal API, requires login)."""
    try:
        from twikit import Client
    except ImportError:
        _post("twikit not installed. Install with: pip install twikit", "error")
        return []

    username = plat_cfg.get("username")
    email = plat_cfg.get("email")
    password = plat_cfg.get("password")
    cookies_file = plat_cfg.get("cookies_file", "twitter_cookies.json")

    if not username or not password:
        _post("Twitter login credentials not configured", "warning")
        return []

    phrases = config.get("intent_phrases", [])
    limit = plat_cfg.get("limit_per_query", 20)
    seen_ids = set(state.get("last_ids", {}).get("twitter", []))
    results = []

    async def _search():
        client = Client("en-US")
        try:
            await client.login(
                auth_info_1=username,
                auth_info_2=email or username,
                password=password,
                cookies_file=cookies_file
            )
        except Exception as e:
            _post(f"Twitter/Twikit login failed: {e}", "error")
            return []

        for phrase in phrases[:5]:  # Limit to avoid rate issues
            try:
                tweets = await client.search_tweet(phrase, "Latest")
                count = 0
                for tweet in tweets:
                    if count >= limit:
                        break
                    tid = getattr(tweet, "id", "")
                    if str(tid) in seen_ids:
                        continue
                    seen_ids.add(str(tid))
                    text = getattr(tweet, "text", "")
                    if matches_intent_phrases(text, phrases):
                        score = score_intent(text, config)
                        results.append({
                            "platform": "twitter",
                            "post_id": str(tid),
                            "title": text[:200],
                            "body": text,
                            "url": f"https://twitter.com/i/web/status/{tid}",
                            "author": str(getattr(getattr(tweet, "user", None), "screen_name", "")),
                            "score": score,
                            "ups": getattr(tweet, "favorite_count", 0),
                            "num_comments": getattr(tweet, "reply_count", 0),
                            "created_utc": str(getattr(tweet, "created_at", ""))
                        })
                    count += 1
                await asyncio.sleep(1.5)
            except Exception as e:
                _post(f"Twitter/Twikit search error: {e}", "warning")
                continue
        return results

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        results = loop.run_until_complete(_search())
        loop.close()
    except Exception as e:
        _post(f"Twitter async error: {e}", "error")

    state.setdefault("last_ids", {})["twitter"] = list(seen_ids)[-2000:]
    return results

def _fetch_twitter_tweepy(plat_cfg: dict, config: dict, state: dict) -> List[dict]:
    """Use official X API v2 via tweepy (requires bearer token)."""
    try:
        import tweepy
    except ImportError:
        _post("tweepy not installed. Install with: pip install tweepy", "error")
        return []

    bearer_token = plat_cfg.get("bearer_token")
    if not bearer_token:
        _post("Twitter bearer token not configured", "warning")
        return []

    phrases = config.get("intent_phrases", [])
    limit = plat_cfg.get("limit_per_query", 20)
    seen_ids = set(state.get("last_ids", {}).get("twitter", []))
    results = []

    try:
        client = tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)
    except Exception as e:
        _post(f"Tweepy client error: {e}", "error")
        return []

    for phrase in phrases[:5]:
        try:
            tweets = client.search_recent_tweets(
                query=phrase,
                max_results=limit,
                tweet_fields=["created_at", "public_metrics", "author_id"]
            )
            if not tweets.data:
                continue
            for tweet in tweets.data:
                if str(tweet.id) in seen_ids:
                    continue
                seen_ids.add(str(tweet.id))
                text = tweet.text
                if matches_intent_phrases(text, phrases):
                    score = score_intent(text, config)
                    results.append({
                        "platform": "twitter",
                        "post_id": str(tweet.id),
                        "title": text[:200],
                        "body": text,
                        "url": f"https://twitter.com/i/web/status/{tweet.id}",
                        "author": str(tweet.author_id) if tweet.author_id else "",
                        "score": score,
                        "ups": tweet.public_metrics.get("like_count", 0),
                        "num_comments": tweet.public_metrics.get("reply_count", 0),
                        "created_utc": str(tweet.created_at) if tweet.created_at else ""
                    })
            time.sleep(1.1)
        except Exception as e:
            _post(f"Tweepy search error: {e}", "error")
            continue

    state.setdefault("last_ids", {})["twitter"] = list(seen_ids)[-2000:]
    return results

# ── LinkedIn fetcher ─────────────────────────────────────────────
def fetch_linkedin(config: dict, state: dict) -> List[dict]:
    """Search LinkedIn for intent signals using RapidAPI."""
    plat_cfg = config.get("platforms", {}).get("linkedin", {})
    if not plat_cfg.get("enabled"):
        return []

    method = plat_cfg.get("method", "rapidapi")
    if method != "rapidapi":
        _post(f"LinkedIn method '{method}' not supported", "warning")
        return []

    return _fetch_linkedin_rapidapi(plat_cfg, config, state)

def _fetch_linkedin_rapidapi(plat_cfg: dict, config: dict, state: dict) -> List[dict]:
    """Use Comprehensive LinkedIn Platform API on RapidAPI."""
    api_key = plat_cfg.get("rapidapi_key")
    host = plat_cfg.get("rapidapi_host", "comprehensive-linkedin-platform.p.rapidapi.com")

    if not api_key:
        _post("LinkedIn RapidAPI key not configured", "warning")
        return []

    phrases = config.get("intent_phrases", [])
    limit = plat_cfg.get("limit_per_query", 20)
    seen_ids = set(state.get("last_ids", {}).get("linkedin", []))
    results = []

    headers = {
        "x-rapidapi-key": api_key,
        "x-rapidapi-host": host
    }

    for phrase in phrases[:3]:  # Be conservative with API calls
        try:
            url = f"https://{host}/api/posts"
            params = {"q": phrase}
            resp = requests.get(url, headers=headers, params=params, timeout=15)
            if resp.status_code != 200:
                _post(f"LinkedIn RapidAPI HTTP {resp.status_code}: {resp.text[:200]}", "warning")
                continue

            data = resp.json()
            items = data.get("results", data.get("items", []))
            count = 0
            for item in items:
                if count >= limit:
                    break
                post_id = str(item.get("id", item.get("post_id", "")))
                if post_id and post_id in seen_ids:
                    continue
                if post_id:
                    seen_ids.add(post_id)

                text = (item.get("title", "") + " " + item.get("text", "") + " " + item.get("content", ""))
                if matches_intent_phrases(text, phrases):
                    score = score_intent(text, config)
                    results.append({
                        "platform": "linkedin",
                        "post_id": post_id,
                        "title": text[:200],
                        "body": text[:500],
                        "url": item.get("url", item.get("permalink", "")),
                        "author": str(item.get("author", {}).get("name", item.get("author_name", ""))),
                        "score": score,
                        "ups": item.get("engagement", item.get("likes", 0)),
                        "num_comments": item.get("comments", 0),
                        "created_utc": str(item.get("created_at", item.get("timestamp", "")))
                    })
                count += 1
            time.sleep(1.1)
        except Exception as e:
            _post(f"LinkedIn RapidAPI error: {e}", "warning")
            continue

    state.setdefault("last_ids", {})["linkedin"] = list(seen_ids)[-2000:]
    return results

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Intent Signal Bot online — monitoring Reddit, X, LinkedIn for intent phrases")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        poll_minutes = int(config.get("poll_interval_minutes", 30))
        state_file = config.get("state_file", "intent_signal_state.json")
        state = load_state(state_file)
        alert_threshold = config.get("intent_scoring", {}).get("alert_threshold", 3)
        all_results = []

        # ── Reddit ──────────────────────────────────────────────
        try:
            reddit_results = fetch_reddit(config, state)
            all_results.extend(reddit_results)
            _post(f"Reddit: {len(reddit_results)} intent signals found", "info")
        except Exception as e:
            _post(f"Reddit scan failed: {e}", "error")

        # ── Twitter/X ───────────────────────────────────────────
        try:
            twitter_results = fetch_twitter(config, state)
            all_results.extend(twitter_results)
            _post(f"Twitter: {len(twitter_results)} intent signals found", "info")
        except Exception as e:
            _post(f"Twitter scan failed: {e}", "error")

        # ── LinkedIn ────────────────────────────────────────────
        try:
            linkedin_results = fetch_linkedin(config, state)
            all_results.extend(linkedin_results)
            _post(f"LinkedIn: {len(linkedin_results)} intent signals found", "info")
        except Exception as e:
            _post(f"LinkedIn scan failed: {e}", "error")

        # ── Sort by score, post top signals ────────────────────
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        high_intent = [r for r in all_results if r.get("score", 0) >= alert_threshold]

        for result in high_intent[:10]:  # Top 10 strongest signals
            level = "warning" if result["score"] >= 5 else "info"
            platform = result["platform"]
            title = result.get("title", "")[:120]
            summary = f"[{platform.upper()}] Intent score {result['score']}: {title}"
            _post(summary, level, result)

        if not high_intent:
            _post("No high-intent signals in this scan", "info")

        save_state(state_file, state)
        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
