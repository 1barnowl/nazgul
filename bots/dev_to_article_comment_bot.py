#!/usr/bin/env python3
"""
dev_to_article_comment_bot.py — Dev.to Article & Comment Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Publishes developer‑focused blog posts and engages in
discussion threads via the Dev.to API. Helps build
authority and drive traffic.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `dev_to_engagement_config.json` in the same directory:

{
  "dev_to": {
    "api_key": "YOUR_DEVTO_API_KEY",
    "username": "your_username"
  },
  "publishing": {
    "enabled": true,
    "articles_file": "dev_to_articles.json"
  },
  "engagement": {
    "enabled": true,
    "tags": ["python", "automation", "devops"],
    "comment_template": "Great article! I've been working on similar ideas and built a toolkit that might interest you: {link}",
    "link": "https://your-product.com",
    "max_comments_per_run": 3,
    "comment_on_own_articles": true,
    "own_article_reply_template": "Thanks for reading! We'd love your feedback on the product: {link}"
  },
  "state_file": "dev_to_engagement_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 60
}

Articles file (`dev_to_articles.json`):
[
  {
    "title": "How to Automate Your Workflow with Python",
    "body_markdown": "Full markdown content...",
    "tags": ["python", "automation"],
    "canonical_url": "https://your-blog.com/original",
    "series": null,
    "scheduled_at": "2025-02-10T09:00:00Z"
  }
]
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "dev_to_article_comment_bot"
BOT_NAME = "Dev.to Article & Comment"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "dev_to_engagement_config.json"
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
        return {
            "published_article_ids": [],
            "commented_article_ids": [],
            "replied_comment_ids": []
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Dev.to API helpers ───────────────────────────────────────────
DEV_API = "https://dev.to/api"

def _dev_headers(api_key: str) -> dict:
    return {
        "api-key": api_key,
        "Content-Type": "application/json"
    }

def dev_post(api_key: str, endpoint: str, body: dict) -> Optional[dict]:
    url = f"{DEV_API}/{endpoint}"
    try:
        resp = requests.post(url, json=body, headers=_dev_headers(api_key), timeout=15)
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            _post(f"Dev.to API error POST {endpoint}: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Dev.to request error: {e}", "error")
        return None

def dev_get(api_key: str, endpoint: str, params: dict = None) -> Optional[dict]:
    url = f"{DEV_API}/{endpoint}"
    try:
        resp = requests.get(url, headers=_dev_headers(api_key), params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"Dev.to API error GET {endpoint}: {resp.status_code}", "error")
            return None
    except Exception as e:
        _post(f"Dev.to request error: {e}", "error")
        return None

# ── Publishing scheduled articles ────────────────────────────────
def publish_article(api_key: str, article: dict) -> Optional[int]:
    """Create an article. Returns article ID."""
    body = {
        "article": {
            "title": article.get("title", ""),
            "body_markdown": article.get("body_markdown", ""),
            "tags": article.get("tags", []),
            "canonical_url": article.get("canonical_url"),
            "series": article.get("series"),
            "published": True
        }
    }
    # Remove None values
    body["article"] = {k:v for k,v in body["article"].items() if v is not None}
    result = dev_post(api_key, "articles", body)
    if result and "id" in result:
        return result["id"]
    return None

def process_scheduled_articles(config: dict, state: dict):
    """Publish articles that are due."""
    if not config.get("publishing", {}).get("enabled", False):
        return
    file_path = config["publishing"].get("articles_file", "dev_to_articles.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            articles = json.load(f)
    except Exception as e:
        _post(f"Error reading articles file: {e}", "error")
        return

    api_key = config["dev_to"]["api_key"]
    now = datetime.now(timezone.utc)
    remaining = []
    published_ids = set(state.get("published_article_ids", []))

    for article in articles:
        scheduled_at_str = article.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(article)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(article)
            continue

        # Generate unique hash for this scheduled article (to avoid republishing)
        item_id = str(hash(json.dumps(article, sort_keys=True)))
        if item_id in published_ids:
            # already processed, remove from file
            continue

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            article_id = publish_article(api_key, article)
            if article_id:
                _post(f"Published article: {article['title'][:60]} (ID {article_id})", "info")
                published_ids.add(item_id)
                # success, remove from queue
                continue
            else:
                _post("Failed to publish article", "error")
        remaining.append(article)

    state["published_article_ids"] = list(published_ids)[-500:]
    # Write remaining articles back
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Engagement: comment on tagged articles ──────────────────────
def get_articles_by_tag(api_key: str, tag: str, limit: int = 10) -> List[dict]:
    """Fetch recent articles for a given tag."""
    params = {
        "tag": tag,
        "per_page": min(limit, 10),
        "state": "fresh"  # get fresh articles
    }
    data = dev_get(api_key, "articles", params)
    if not data:
        return []
    return data

def comment_on_article(api_key: str, article_id: int, comment: str) -> Optional[int]:
    """Add a comment to an article. Returns comment ID."""
    body = {"comment": {"body_markdown": comment}}
    result = dev_post(api_key, f"articles/{article_id}/comments", body)
    if result and "id" in result:
        return result["id"]
    return None

def process_engagement(config: dict, state: dict):
    """Search for articles by tags, leave helpful comments."""
    if not config.get("engagement", {}).get("enabled", False):
        return
    api_key = config["dev_to"]["api_key"]
    tags = config["engagement"].get("tags", [])
    max_comments = int(config["engagement"].get("max_comments_per_run", 3))
    comment_template = config["engagement"].get("comment_template", "")
    link = config["engagement"].get("link", "")
    if not tags or not comment_template:
        return

    commented_article_ids = set(state.get("commented_article_ids", []))
    new_comments = 0

    for tag in tags:
        articles = get_articles_by_tag(api_key, tag, max_comments * 2)
        for article in articles:
            article_id = article.get("id")
            if not article_id or article_id in commented_article_ids:
                continue
            # Check if article contains any of our keywords? Not implemented; we simply comment on all fresh ones.
            # To avoid spam, we can optionally check that the article's title doesn't contain certain words, but skipping.
            comment_text = comment_template.replace("{link}", link)
            comment_id = comment_on_article(api_key, article_id, comment_text)
            if comment_id:
                _post(f"Commented on article {article_id} ('{article.get('title','')[:50]}...')", "info")
                commented_article_ids.add(article_id)
                new_comments += 1
                time.sleep(1)  # rate limit
                if new_comments >= max_comments:
                    break
        if new_comments >= max_comments:
            break
    state["commented_article_ids"] = list(commented_article_ids)[-500:]

# ── Reply to comments on own articles ────────────────────────────
def get_comments_on_article(api_key: str, article_id: int, limit: int = 20) -> List[dict]:
    endpoint = f"articles/{article_id}/comments?per_page={limit}"
    return dev_get(api_key, endpoint)

def reply_to_comment(api_key: str, comment_id: int, body_markdown: str) -> Optional[int]:
    """Reply to a specific comment (threaded). The API endpoint is /comments/:id/replies"""
    endpoint = f"comments/{comment_id}/replies"
    return comment_on_article(api_key, comment_id, body_markdown)  # actually it's comments/id/replies

def process_own_article_comments(config: dict, state: dict):
    """Monitor comments on our own articles (published by this bot) and reply."""
    if not config.get("engagement", {}).get("comment_on_own_articles", False):
        return
    api_key = config["dev_to"]["api_key"]
    reply_template = config["engagement"].get("own_article_reply_template", "Thanks for your comment!")
    link = config["engagement"].get("link", "")

    # Get articles we published (from state)
    published_ids = set(state.get("published_article_ids", []))
    replied_comment_ids = set(state.get("replied_comment_ids", []))

    for article_id in published_ids:
        try:
            article_id_int = int(article_id)  # if stored as int
        except (ValueError, TypeError):
            continue
        comments = get_comments_on_article(api_key, article_id_int, limit=20)
        if not isinstance(comments, list):
            continue
        for comment in comments:
            comment_id = comment.get("id")
            if not comment_id or comment_id in replied_comment_ids:
                continue
            # Skip own comments? We could check comment.user.username vs our username
            username = config["dev_to"]["username"]
            if comment.get("user", {}).get("username") == username:
                continue
            reply_body = reply_template.replace("{link}", link)
            if reply_to_comment(api_key, comment_id, reply_body):
                _post(f"Replied to comment {comment_id} on article {article_id}", "info")
                replied_comment_ids.add(comment_id)
                time.sleep(1)
                # Limit replies per run
                if len(replied_comment_ids) >= 10:
                    break
        if len(replied_comment_ids) >= 10:
            break
    state["replied_comment_ids"] = list(replied_comment_ids)[-500:]

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Dev.to Article & Comment Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        api_key = config.get("dev_to", {}).get("api_key")
        if not api_key:
            _post("Dev.to API key missing", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "dev_to_engagement_state.json")
        state = load_state(state_file)

        # Publishing
        process_scheduled_articles(config, state)

        # Engagement on other articles
        process_engagement(config, state)

        # Reply to comments on own articles
        process_own_article_comments(config, state)

        save_state(state_file, state)

        poll_min = int(config.get("poll_interval_minutes", 60))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
