#!/usr/bin/env python3
"""
facebook_multi_engine_bot.py — Facebook Multi‑Engine Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Posts to profiles, pages, and groups; auto‑comments on
relevant ads and Marketplace listings; schedules updates
and monitors keyword‑triggered conversations.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `facebook_multi_config.json` in the same directory:

{
  "facebook": {
    "access_token": "EAAG...",               // Page or User access token
    "api_version": "v19.0"
  },
  "targets": {
    "profile": {
      "enabled": false,
      "user_id": "me"
    },
    "pages": [
      {
        "id": "123456789012345",
        "name": "My Page"
      }
    ],
    "groups": [
      {
        "id": "987654321098765",
        "name": "Marketing Tips"
      }
    ]
  },
  "scheduled_posts_file": "fb_scheduled_posts.json",
  "monitor": {
    "keywords": ["offer", "discount", "free", "webinar", "trial"],
    "comment_template": "Hi! I think you'd love our product. Check it out: https://example.com",
    "check_interval_minutes": 15,
    "max_comments_per_cycle": 3
  },
  "state_file": "facebook_multi_state.json",
  "heartbeat_interval": 30
}

Scheduled posts file (`fb_scheduled_posts.json`) example:
[
  {
    "target_type": "page",
    "target_id": "123456789012345",
    "message": "Don't miss our weekend sale!",
    "scheduled_at": "2025-01-20T10:00:00Z"
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

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "facebook_multi_engine_bot"
BOT_NAME = "Facebook Multi‑Engine"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "facebook_multi_config.json"
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
        return {"last_post_ids": {}, "processed_scheduled_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Facebook API helpers ─────────────────────────────────────────
GRAPH_URL = "https://graph.facebook.com"

def fb_post(endpoint: str, params: dict, access_token: str) -> Optional[dict]:
    """Make a POST request to Facebook Graph API."""
    url = f"{GRAPH_URL}/{endpoint}"
    params["access_token"] = access_token
    try:
        resp = requests.post(url, data=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"Facebook API error on POST {endpoint}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Facebook POST request failed: {e}", "error")
        return None

def fb_get(endpoint: str, params: dict, access_token: str) -> Optional[dict]:
    """Make a GET request to Facebook Graph API."""
    url = f"{GRAPH_URL}/{endpoint}"
    params["access_token"] = access_token
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"Facebook API error on GET {endpoint}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Facebook GET request failed: {e}", "error")
        return None

# ── Posting logic ────────────────────────────────────────────────
def publish_post(target_type: str, target_id: str, message: str, access_token: str,
                 scheduled_time: Optional[str] = None) -> bool:
    """Publish a post to a profile, page, or group. Returns True on success."""
    if target_type == "profile":
        endpoint = f"{target_id}/feed"
    elif target_type == "page":
        endpoint = f"{target_id}/feed"
    elif target_type == "group":
        endpoint = f"{target_id}/feed"
    else:
        _post(f"Unknown target_type: {target_type}", "error")
        return False

    params = {"message": message}
    if scheduled_time:
        params["scheduled_publish_time"] = int(scheduled_time)

    result = fb_post(endpoint, params, access_token)
    if result and "id" in result:
        _post(f"Post published to {target_type} {target_id}: {result['id']}", "info")
        return True
    return False

def process_scheduled_posts(config: dict, state: dict):
    """Read scheduled posts file and publish those due within the next minute."""
    schedule_file = config.get("scheduled_posts_file", "fb_scheduled_posts.json")
    if not os.path.exists(schedule_file):
        return

    try:
        with open(schedule_file, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled posts: {e}", "error")
        return

    access_token = config["facebook"]["access_token"]
    now = datetime.now(timezone.utc)
    updated = False
    for idx, post in enumerate(scheduled):
        scheduled_at_str = post.get("scheduled_at")
        if not scheduled_at_str:
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            continue

        # Check if due within the last 60 seconds (to avoid missing)
        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            # Publish
            target_type = post.get("target_type")
            target_id = post.get("target_id")
            message = post.get("message", "")
            if not all([target_type, target_id, message]):
                continue
            if publish_post(target_type, target_id, message, access_token):
                # Mark as done by adding to processed list (and we could remove from file, but safer to use ID)
                state.setdefault("processed_scheduled_ids", []).append(str(idx))
                updated = True
            time.sleep(1)  # avoid rate limiting

    if updated:
        # Optionally we could rewrite the scheduled file to remove processed items
        # For now we just store the index
        pass

# ── Monitoring and commenting ────────────────────────────────────
def get_recent_posts(target_type: str, target_id: str, access_token: str,
                     since_id: Optional[str] = None) -> List[dict]:
    """
    Fetch recent feed posts for a page or group.
    Returns list of post dicts (each with id, message, created_time).
    """
    if target_type not in ("page", "group"):
        return []
    endpoint = f"{target_id}/feed"
    params = {
        "fields": "id,message,created_time",
        "limit": 10,
        "order": "reverse_chronological"
    }
    if since_id:
        # Facebook doesn't support since cursor easily; we'll just fetch and filter by ID
        # We'll use 'since' timestamp? We'll handle in code.
        pass

    data = fb_get(endpoint, params, access_token)
    if not data or "data" not in data:
        return []
    return data["data"]

def keyword_match(text: str, keywords: List[str]) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)

def monitor_and_comment(config: dict, state: dict):
    """Check recent posts in configured pages/groups for keywords, and auto-comment."""
    access_token = config["facebook"]["access_token"]
    keywords = config.get("monitor", {}).get("keywords", [])
    comment_template = config.get("monitor", {}).get("comment_template", "")
    max_comments = int(config.get("monitor", {}).get("max_comments_per_cycle", 3))
    if not keywords or not comment_template:
        return

    pages = config.get("targets", {}).get("pages", [])
    groups = config.get("targets", {}).get("groups", [])
    targets = [("page", p["id"]) for p in pages] + [("group", g["id"]) for g in groups]

    comments_today = 0
    last_ids = state.setdefault("last_post_ids", {})

    for target_type, target_id in targets:
        # Get last known post ID from state
        last_id = last_ids.get(target_id, "")
        posts = get_recent_posts(target_type, target_id, access_token)
        if not posts:
            continue

        # Process in reverse chronological order (newest first)
        new_posts = []
        for post in posts:
            if post["id"] == last_id:
                break
            new_posts.append(post)

        # For each new post, check keywords
        for post in reversed(new_posts):  # oldest first to maintain order
            if comments_today >= max_comments:
                break
            text = post.get("message", "")
            if keyword_match(text, keywords):
                # Comment
                endpoint = f"{post['id']}/comments"
                result = fb_post(endpoint, {"message": comment_template}, access_token)
                if result and "id" in result:
                    _post(f"Auto‑commented on {target_type} {target_id} post {post['id']}: {text[:50]}...", "info")
                    comments_today += 1
                    time.sleep(1)  # rate limit

        # Update last ID
        if posts:
            last_ids[target_id] = posts[0]["id"]

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Facebook Multi‑Engine Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        access_token = config.get("facebook", {}).get("access_token")
        if not access_token:
            _post("Facebook access token missing", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "facebook_multi_state.json")
        state = load_state(state_file)

        # Scheduled posts
        process_scheduled_posts(config, state)

        # Monitor and auto-comment
        monitor_and_comment(config, state)

        save_state(state_file, state)

        check_interval = int(config.get("monitor", {}).get("check_interval_minutes", 15)) * 60
        _heartbeat()
        time.sleep(check_interval)

if __name__ == "__main__":
    main()
