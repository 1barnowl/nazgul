#!/usr/bin/env python3
"""
tumblr_multimedia_blast_bot.py — Tumblr Multimedia Blast Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Queues image and text posts with community tags, reblogs
and comments on trending visual content to funnel traffic
to a primary link. Uses the Tumblr API v2.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests requests-oauthlib

Configuration
─────────────
Place `tumblr_blast_config.json` in the same directory:

{
  "tumblr": {
    "consumer_key": "YOUR_CONSUMER_KEY",
    "consumer_secret": "YOUR_CONSUMER_SECRET",
    "oauth_token": "YOUR_OAUTH_TOKEN",
    "oauth_token_secret": "YOUR_OAUTH_TOKEN_SECRET",
    "blog_identifier": "your-blog.tumblr.com"       // your blog's hostname
  },
  "publishing": {
    "enabled": true,
    "queue_file": "tumblr_scheduled_queue.json"
  },
  "engagement": {
    "enabled": true,
    "tags": ["marketing", "design", "photography"],
    "comment_template": "Love this! Check out our latest content: {link}",
    "link": "https://your-site.com",
    "max_reblogs_per_run": 5,
    "check_interval_minutes": 60
  },
  "state_file": "tumblr_blast_state.json",
  "heartbeat_interval": 30
}

Scheduled queue file (`tumblr_scheduled_queue.json`) format:
[
  {
    "type": "photo",          // "text" or "photo"
    "caption": "Check out our new design!",
    "source": "https://example.com/image.jpg",   // for photo posts
    "link": "https://your-site.com",
    "tags": "design, art, illustration",
    "scheduled_at": "2025-01-28T15:00:00Z"
  },
  {
    "type": "text",
    "title": "Our Latest Blog Post",
    "body": "Read the full article here: https://your-site.com",
    "tags": "blog, marketing",
    "scheduled_at": "2025-01-29T09:00:00Z"
  }
]
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from requests_oauthlib import OAuth1Session

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "tumblr_multimedia_blast_bot"
BOT_NAME = "Tumblr Multimedia Blast"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "tumblr_blast_config.json"
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
        return {"reblogged_posts": [], "scheduled_posted_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Tumblr API session ───────────────────────────────────────────
def get_tumblr_session(config: dict) -> OAuth1Session:
    tum_cfg = config["tumblr"]
    return OAuth1Session(
        client_key=tum_cfg["consumer_key"],
        client_secret=tum_cfg["consumer_secret"],
        resource_owner_key=tum_cfg["oauth_token"],
        resource_owner_secret=tum_cfg["oauth_token_secret"]
    )

# ── Publishing: create posts ─────────────────────────────────────
def create_post(session: OAuth1Session, blog_identifier: str,
                post_type: str, params: dict) -> Optional[str]:
    """Create a Tumblr post. Returns post ID or None."""
    url = f"https://api.tumblr.com/v2/blog/{blog_identifier}/post"
    params["type"] = post_type
    try:
        resp = session.post(url, data=params, timeout=15)
        if resp.status_code in (200, 201):
            data = resp.json()
            return str(data["response"]["id"])
        else:
            _post(f"Tumblr post error: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Tumblr post request failed: {e}", "error")
        return None

def process_scheduled_queue(config: dict, state: dict):
    """Read the queue file and create posts due within the next minute."""
    if not config.get("publishing", {}).get("enabled", False):
        return
    queue_file = config["publishing"]["queue_file"]
    if not os.path.exists(queue_file):
        return
    try:
        with open(queue_file, "r") as f:
            queue = json.load(f)
    except Exception as e:
        _post(f"Error reading queue file: {e}", "error")
        return

    if not queue:
        return

    session = get_tumblr_session(config)
    blog_identifier = config["tumblr"]["blog_identifier"]
    now = datetime.now(timezone.utc)
    remaining = []
    posted_ids = set(state.get("scheduled_posted_ids", []))

    for item in queue:
        scheduled_at_str = item.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(item)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(item)
            continue

        item_id = str(hash(json.dumps(item, sort_keys=True)))
        if item_id in posted_ids:
            continue  # already posted

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            post_type = item.get("type", "text")
            params = {}
            if post_type == "photo":
                params["caption"] = item.get("caption", "")
                params["source"] = item.get("source", "")
                if item.get("link"):
                    params["link"] = item["link"]
            else:  # text
                params["title"] = item.get("title", "")
                params["body"] = item.get("body", "")

            tags = item.get("tags", "")
            if tags:
                params["tags"] = tags

            post_id = create_post(session, blog_identifier, post_type, params)
            if post_id:
                _post(f"Queued {post_type} post: {post_id}", "info", {"post_id": post_id})
                posted_ids.add(item_id)
                # success: do not keep in file
                continue
            else:
                _post("Failed to create post, keeping for retry", "error")
        remaining.append(item)

    # Update state
    state["scheduled_posted_ids"] = list(posted_ids)[-500:]
    # Write remaining items back
    with open(queue_file, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Engagement: reblog trending tagged content ───────────────────
def get_tagged_posts(session: OAuth1Session, tag: str, limit: int = 20) -> List[dict]:
    """Get recent posts with a given tag."""
    url = f"https://api.tumblr.com/v2/tagged"
    params = {"tag": tag, "limit": limit}
    try:
        resp = session.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()["response"]
        else:
            _post(f"Tag fetch error for '{tag}': {resp.status_code}", "warning")
            return []
    except Exception as e:
        _post(f"Tag fetch request failed: {e}", "error")
        return []

def reblog_post(session: OAuth1Session, blog_identifier: str,
                post_id: str, reblog_key: str, comment: str = "") -> Optional[str]:
    """Reblog a post with an optional comment. Returns the new post ID."""
    url = f"https://api.tumblr.com/v2/blog/{blog_identifier}/post/reblog"
    data = {
        "id": post_id,
        "reblog_key": reblog_key,
        "comment": comment
    }
    try:
        resp = session.post(url, data=data, timeout=15)
        if resp.status_code in (200, 201):
            return str(resp.json()["response"]["id"])
        else:
            _post(f"Reblog error: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Reblog request failed: {e}", "error")
        return None

def process_engagement(config: dict, state: dict):
    """Find trending tagged posts and reblog with a comment."""
    if not config.get("engagement", {}).get("enabled", False):
        return

    session = get_tumblr_session(config)
    blog_identifier = config["tumblr"]["blog_identifier"]
    tags = config["engagement"].get("tags", [])
    comment_template = config["engagement"].get("comment_template", "")
    link = config["engagement"].get("link", "")
    max_reblogs = int(config["engagement"].get("max_reblogs_per_run", 5))
    reblogged = set(state.get("reblogged_posts", []))

    count = 0
    for tag in tags:
        posts = get_tagged_posts(session, tag, limit=10)
        for post in posts:
            if count >= max_reblogs:
                return
            post_id = str(post.get("id"))
            if post_id in reblogged:
                continue
            reblog_key = post.get("reblog_key")
            if not reblog_key:
                continue
            # Build comment
            comment = comment_template.replace("{link}", link) if comment_template else ""
            new_id = reblog_post(session, blog_identifier, post_id, reblog_key, comment)
            if new_id:
                _post(f"Reblogged post {post_id} with comment", "info", {"new_post_id": new_id})
                reblogged.add(post_id)
                count += 1
                time.sleep(1)  # rate limit
            else:
                _post(f"Failed to reblog {post_id}", "error")
        # Stop if max reached
        if count >= max_reblogs:
            break
    # Update state (keep last 1000 IDs)
    state["reblogged_posts"] = list(reblogged)[-1000:]

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Tumblr Multimedia Blast Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "tumblr_blast_state.json")
        state = load_state(state_file)

        # Scheduled queue posts
        process_scheduled_queue(config, state)

        # Engagement (reblogging)
        process_engagement(config, state)

        save_state(state_file, state)

        check_min = int(config.get("engagement", {}).get("check_interval_minutes", 60)) * 60
        _heartbeat()
        time.sleep(check_min)

if __name__ == "__main__":
    main()
