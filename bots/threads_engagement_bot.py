#!/usr/bin/env python3
"""
threads_engagement_bot.py — Threads by Meta Engagement Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Posts text‑first updates and replies to trending threads
via the Threads API (Instagram Graph API).  Builds community
through conversational, direct engagement.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `threads_engagement_config.json` in the same directory:

{
  "threads": {
    "access_token": "EAAG...",               // Instagram Graph API access token
    "user_id": "17841400000000000"           // Threads user ID (numeric)
  },
  "publishing": {
    "enabled": true,
    "posts_file": "threads_scheduled_posts.json"
  },
  "monitoring": {
    "enabled": true,
    "target_profiles": [
      {
        "user_id": "17841400000000001",
        "name": "Example Influencer"
      }
    ],
    "reply_template": "Great thread! We've been exploring similar topics. Would love to connect.",
    "max_replies_per_run": 3,
    "check_interval_minutes": 60
  },
  "state_file": "threads_engagement_state.json",
  "heartbeat_interval": 30
}

Scheduled posts file (`threads_scheduled_posts.json`):
[
  {
    "text": "Today's thought: AI is reshaping marketing.",
    "scheduled_at": "2025-01-21T15:00:00Z"
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

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "threads_engagement_bot"
BOT_NAME = "Threads Engagement"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "threads_engagement_config.json"
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
        return {"replied_threads": [], "published_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Threads API helpers ──────────────────────────────────────────
GRAPH_URL = "https://graph.threads.net/v1.0"  # or graph.facebook.com

def threads_post(endpoint: str, params: dict, access_token: str) -> Optional[dict]:
    """Send a POST request to the Threads API."""
    url = f"{GRAPH_URL}/{endpoint}"
    params["access_token"] = access_token
    try:
        resp = requests.post(url, data=params, timeout=15)
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            _post(f"Threads API error on POST {endpoint}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Threads POST request error: {e}", "error")
        return None

def threads_get(endpoint: str, params: dict, access_token: str) -> Optional[dict]:
    """Send a GET request to the Threads API."""
    url = f"{GRAPH_URL}/{endpoint}"
    params["access_token"] = access_token
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"Threads API error on GET {endpoint}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Threads GET request error: {e}", "error")
        return None

# ── Publishing ──────────────────────────────────────────────────
def create_thread(user_id: str, text: str, access_token: str) -> Optional[str]:
    """Create a new thread post. Returns the thread ID if successful."""
    endpoint = f"{user_id}/threads"
    params = {
        "text": text
    }
    result = threads_post(endpoint, params, access_token)
    if result and "id" in result:
        return result["id"]
    return None

def process_scheduled_posts(config: dict, state: dict):
    """Publish due scheduled posts."""
    if not config.get("publishing", {}).get("enabled", False):
        return
    posts_file = config["publishing"].get("posts_file", "threads_scheduled_posts.json")
    if not os.path.exists(posts_file):
        return
    try:
        with open(posts_file, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled posts: {e}", "error")
        return

    access_token = config["threads"]["access_token"]
    user_id = config["threads"]["user_id"]
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
            thread_id = create_thread(user_id, text, access_token)
            if thread_id:
                _post(f"Published thread: {text[:60]}", "info", {"thread_id": thread_id})
                # Success: remove from list
                continue
            else:
                _post("Failed to publish thread, keeping for retry", "error")
        remaining.append(post)
    # Write back remaining posts
    with open(posts_file, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Monitoring & replying ────────────────────────────────────────
def get_user_threads(user_id: str, access_token: str, limit: int = 5) -> List[dict]:
    """Retrieve recent threads of a given user."""
    endpoint = f"{user_id}/threads"
    params = {
        "fields": "id,text,timestamp",
        "limit": limit
    }
    data = threads_get(endpoint, params, access_token)
    if data and "data" in data:
        return data["data"]
    return []

def reply_to_thread(user_id: str, thread_id: str, reply_text: str, access_token: str) -> Optional[str]:
    """Reply to a thread. Returns the reply ID if successful."""
    endpoint = f"{user_id}/replies"
    params = {
        "reply_to_id": thread_id,
        "text": reply_text
    }
    result = threads_post(endpoint, params, access_token)
    if result and "id" in result:
        return result["id"]
    return None

def process_monitoring(config: dict, state: dict):
    """Check target profiles for new threads and reply."""
    if not config.get("monitoring", {}).get("enabled", False):
        return
    access_token = config["threads"]["access_token"]
    user_id = config["threads"]["user_id"]  # our own ID (for replies)
    targets = config["monitoring"].get("target_profiles", [])
    max_replies = int(config["monitoring"].get("max_replies_per_run", 3))
    reply_template = config["monitoring"].get("reply_template", "Great thread!")

    replied_ids = set(state.get("replied_threads", []))
    new_replies = 0

    for target in targets:
        target_id = target.get("user_id")
        if not target_id:
            continue
        threads = get_user_threads(target_id, access_token)
        for thread in threads:
            thread_id = thread.get("id")
            if not thread_id or thread_id in replied_ids:
                continue
            reply_text = reply_template
            reply_id = reply_to_thread(user_id, thread_id, reply_text, access_token)
            if reply_id:
                _post(f"Replied to thread {thread_id} from {target.get('name', target_id)}", "info")
                replied_ids.add(thread_id)
                state["replied_threads"] = list(replied_ids)[-500:]  # trim
                new_replies += 1
                time.sleep(1)
                if new_replies >= max_replies:
                    return
            else:
                _post(f"Failed to reply to thread {thread_id}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Threads Engagement Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        access_token = config.get("threads", {}).get("access_token")
        if not access_token:
            _post("Missing Threads access token", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "threads_engagement_state.json")
        state = load_state(state_file)

        # Scheduled posts
        process_scheduled_posts(config, state)

        # Monitoring & replying
        process_monitoring(config, state)

        save_state(state_file, state)

        check_min = int(config.get("monitoring", {}).get("check_interval_minutes", 60)) * 60
        _heartbeat()
        time.sleep(check_min)

if __name__ == "__main__":
    main()
