#!/usr/bin/env python3
"""
facebook_manychat_bot.py — Facebook Messenger / Chatbot Funnels
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors comments on specific Facebook posts or recent posts,
triggers a ManyChat flow that sends a DM sequence to the
commenter, and optionally replies to the comment publicly.
All actions are reported to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `manychat_config.json` in the same directory:

{
  "facebook": {
    "page_access_token": "EAAG...",
    "page_id": "123456789012345"
  },
  "manychat": {
    "api_key": "YOUR_MANYCHAT_API_KEY",
    "flow_ns": "ad_comment_flow"             // ManyChat flow namespace
  },
  "monitoring": {
    "post_ids": ["123456789012345_987654321098765"],   // specific post IDs to monitor; leave empty to watch recent posts
    "auto_reply_comment": "Thanks! Check your DMs for a special offer.",
    "max_comments_per_run": 10,
    "cooldown_minutes": 5
  },
  "state_file": "manychat_state.json",
  "heartbeat_interval": 30
}
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
BOT_ID = "facebook_manychat_bot"
BOT_NAME = "Facebook Messenger / Chatbot Funnels"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "manychat_config.json"
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
        return {"processed_comment_ids": [], "last_run": None}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Facebook Graph API ───────────────────────────────────────────
GRAPH_URL = "https://graph.facebook.com"

def fb_get(endpoint: str, params: dict, access_token: str) -> Optional[dict]:
    url = f"{GRAPH_URL}/{endpoint}"
    params["access_token"] = access_token
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"FB API error on GET {endpoint}: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"FB API request error: {e}", "error")
        return None

def fb_post(endpoint: str, data: dict, access_token: str) -> Optional[dict]:
    url = f"{GRAPH_URL}/{endpoint}"
    data["access_token"] = access_token
    try:
        resp = requests.post(url, data=data, timeout=15)
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            _post(f"FB API error on POST {endpoint}: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"FB API request error: {e}", "error")
        return None

def get_comments_on_post(post_id: str, access_token: str, limit: int = 25) -> List[dict]:
    """Return recent comments on a post."""
    endpoint = f"{post_id}/comments"
    params = {
        "fields": "id,message,from,created_time",
        "limit": limit,
        "order": "reverse_chronological"
    }
    data = fb_get(endpoint, params, access_token)
    if data and "data" in data:
        return data["data"]
    return []

def get_recent_posts(page_id: str, access_token: str, limit: int = 5) -> List[str]:
    """Return IDs of the most recent posts on the page."""
    endpoint = f"{page_id}/posts"
    params = {"limit": limit, "fields": "id"}
    data = fb_get(endpoint, params, access_token)
    if data and "data" in data:
        return [p["id"] for p in data["data"]]
    return []

# ── ManyChat API ─────────────────────────────────────────────────
MANYCHAT_API = "https://api.manychat.com/fb"

def manychat_send_flow(api_key: str, subscriber_id: str, flow_ns: str) -> bool:
    """Trigger a ManyChat flow for the given subscriber PSID."""
    url = f"{MANYCHAT_API}/sending/sendFlow"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "subscriber_id": subscriber_id,
        "flow_ns": flow_ns
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=10)
        if resp.status_code in (200, 201):
            return True
        else:
            _post(f"ManyChat API error: {resp.status_code} {resp.text[:200]}", "error")
            return False
    except Exception as e:
        _post(f"ManyChat request failed: {e}", "error")
        return False

# ── Main processing ─────────────────────────────────────────────
def process_comments(config: dict, state: dict):
    fb_cfg = config["facebook"]
    manychat_cfg = config["manychat"]
    monitoring = config.get("monitoring", {})

    access_token = fb_cfg["page_access_token"]
    page_id = fb_cfg["page_id"]
    api_key = manychat_cfg["api_key"]
    flow_ns = manychat_cfg["flow_ns"]

    # Check rate limiting
    cooldown_min = int(monitoring.get("cooldown_minutes", 5))
    last_run_str = state.get("last_run")
    if last_run_str:
        last_run = datetime.fromisoformat(last_run_str)
        if datetime.now(timezone.utc) - last_run < timedelta(minutes=cooldown_min):
            _post("Cooldown active, skipping", "info")
            return

    processed_ids = set(state.get("processed_comment_ids", []))
    max_comments = int(monitoring.get("max_comments_per_run", 10))

    post_ids = monitoring.get("post_ids", [])
    if not post_ids:
        # Fallback to recent posts
        post_ids = get_recent_posts(page_id, access_token, limit=5)

    if not post_ids:
        _post("No posts to monitor", "info")
        return

    new_processed = 0
    for post_id in post_ids:
        comments = get_comments_on_post(post_id, access_token)
        for comment in comments:
            if new_processed >= max_comments:
                break
            comment_id = comment["id"]
            if comment_id in processed_ids:
                continue
            # Get the commenter's PSID (Facebook user ID)
            from_data = comment.get("from", {})
            user_id = from_data.get("id")
            if not user_id:
                _post(f"No user ID in comment {comment_id}", "warning")
                processed_ids.add(comment_id)
                continue

            # Optionally reply to the comment publicly
            reply_text = monitoring.get("auto_reply_comment")
            if reply_text:
                # Facebook Graph API endpoint to reply to a comment
                fb_post(f"{comment_id}/comments", {"message": reply_text}, access_token)

            # Trigger ManyChat flow
            success = manychat_send_flow(api_key, user_id, flow_ns)
            if success:
                _post(f"ManyChat flow triggered for user {user_id} (comment {comment_id})", "info")
            else:
                _post(f"Failed to trigger ManyChat for user {user_id}", "error")

            processed_ids.add(comment_id)
            new_processed += 1
            time.sleep(1)  # respect rate limits

    # Trim processed IDs list to last 2000
    state["processed_comment_ids"] = list(processed_ids)[-2000:]
    state["last_run"] = datetime.now(timezone.utc).isoformat()

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Facebook Messenger / Chatbot Funnels Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "manychat_state.json")
        state = load_state(state_file)

        process_comments(config, state)

        save_state(state_file, state)
        _heartbeat()
        time.sleep(30)  # check every 30 seconds

if __name__ == "__main__":
    main()
