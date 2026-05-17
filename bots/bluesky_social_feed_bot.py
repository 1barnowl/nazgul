#!/usr/bin/env python3
"""
bluesky_social_feed_bot.py — Bluesky Social Feed Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Leverages the AT Protocol to post scheduled updates and
auto‑reply to relevant skeets based on keyword searches.
All actions are reported to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install atproto requests

Configuration
─────────────
Place `bluesky_social_config.json` in the same directory:

{
  "bluesky": {
    "handle": "your-handle.bsky.social",
    "app_password": "your-app-password"
  },
  "publishing": {
    "enabled": true,
    "posts_file": "bluesky_scheduled_posts.json"
  },
  "engagement": {
    "enabled": true,
    "keywords": ["marketing", "growth", "AI"],
    "reply_template": "Interesting perspective! We've been exploring similar ideas. Follow us for more insights.",
    "max_replies_per_run": 3,
    "avoid_handles": ["spam_account.bsky.social"]
  },
  "state_file": "bluesky_social_state.json",
  "heartbeat_interval": 30,
  "poll_interval_seconds": 300
}

Scheduled posts file (`bluesky_scheduled_posts.json`):
[
  {
    "text": "Today's thought: AI is reshaping the marketing landscape.",
    "scheduled_at": "2025-01-22T14:00:00Z"
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
from atproto import Client

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "bluesky_social_feed_bot"
BOT_NAME = "Bluesky Social Feed"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "bluesky_social_config.json"
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
        return {"replied_to": [], "published_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Bluesky client ───────────────────────────────────────────────
def get_bluesky_client(config: dict) -> Client:
    bs_cfg = config["bluesky"]
    client = Client()
    client.login(bs_cfg["handle"], bs_cfg["app_password"])
    return client

# ── Publishing ──────────────────────────────────────────────────
def create_skeet(client: Client, text: str) -> Optional[str]:
    """Post a text skeet. Returns the URI of the created post."""
    try:
        resp = client.send_post(text)
        return resp.uri
    except Exception as e:
        _post(f"Failed to create skeet: {e}", "error")
        return None

def process_scheduled_posts(config: dict, state: dict):
    """Publish due scheduled posts."""
    if not config.get("publishing", {}).get("enabled", False):
        return
    posts_file = config["publishing"].get("posts_file", "bluesky_scheduled_posts.json")
    if not os.path.exists(posts_file):
        return
    try:
        with open(posts_file, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled posts: {e}", "error")
        return

    client = get_bluesky_client(config)
    now = datetime.now(timezone.utc)
    remaining = []
    for post in scheduled:
        scheduled_at_str = post.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(post)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(post)
            continue
        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            text = post.get("text", "")
            uri = create_skeet(client, text)
            if uri:
                _post(f"Published skeet: {text[:60]}", "info", {"uri": uri})
                # success, remove from list
                continue
            else:
                _post("Failed to publish skeet, retrying later", "error")
        remaining.append(post)
    # Write back remaining posts
    with open(posts_file, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Engagement: Keyword search & auto‑reply ──────────────────────
def search_and_reply(client: Client, config: dict, state: dict):
    """Search for recent posts containing keywords and reply."""
    if not config.get("engagement", {}).get("enabled", False):
        return
    keywords = config["engagement"].get("keywords", [])
    if not keywords:
        return
    reply_template = config["engagement"]["reply_template"]
    max_replies = int(config["engagement"].get("max_replies_per_run", 3))
    avoid_handles = set(handle.lower() for handle in config["engagement"].get("avoid_handles", []))

    replied_to = set(state.get("replied_to", []))
    new_replies = 0

    for keyword in keywords:
        try:
            # AT Protocol search: app.bsky.feed.searchPosts
            response = client.app.bsky.feed.search_posts(
                {"q": keyword, "limit": 5}
            )
            posts = response.posts
            for post in posts:
                uri = post.uri
                author_handle = post.author.handle.lower()
                if uri in replied_to or author_handle in avoid_handles:
                    continue
                # Reply to the post
                # AT Protocol reply requires root and parent references
                root_uri = post.record.reply.root.uri if hasattr(post.record.reply, 'root') else uri
                parent_uri = uri
                reply_ref = {
                    "root": {"uri": root_uri, "cid": post.record.reply.root.cid if hasattr(post.record.reply, 'root') else post.cid},
                    "parent": {"uri": parent_uri, "cid": post.cid}
                }
                try:
                    client.send_post(text=reply_template, reply_to=reply_ref)
                    _post(f"Replied to skeet {uri} from @{author_handle}", "info")
                    replied_to.add(uri)
                    state["replied_to"] = list(replied_to)[-500:]  # trim
                    new_replies += 1
                    time.sleep(1)  # rate limit
                    if new_replies >= max_replies:
                        return
                except Exception as e:
                    _post(f"Failed to reply to {uri}: {e}", "error")
        except Exception as e:
            _post(f"Search for keyword '{keyword}' failed: {e}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Bluesky Social Feed Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "bluesky_social_state.json")
        state = load_state(state_file)

        # Only initialise client if needed
        try:
            client = get_bluesky_client(config)
        except Exception as e:
            _post(f"Bluesky login failed: {e}", "error")
            time.sleep(300)
            continue

        # Scheduled posts
        process_scheduled_posts(config, state)

        # Engagement
        search_and_reply(client, config, state)

        save_state(state_file, state)

        poll_interval = int(config.get("poll_interval_seconds", 300))
        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
