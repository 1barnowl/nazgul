#!/usr/bin/env python3
"""
vk_community_bot.py — VKontakte Community Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Posts updates, engages in group discussions, and
auto‑replies to comments on the largest Russian
social network via VK API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `vk_community_config.json` in the same directory:

{
  "vk": {
    "access_token": "YOUR_SERVICE_ACCESS_TOKEN",
    "group_id": 123456789,
    "api_version": "5.199"
  },
  "publishing": {
    "enabled": true,
    "scheduled_posts_file": "vk_scheduled_posts.json"
  },
  "auto_reply": {
    "enabled": true,
    "reply_template": "Thank you for your comment! We appreciate your feedback.",
    "max_replies_per_run": 5,
    "cooldown_minutes": 60
  },
  "state_file": "vk_community_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 15
}

Scheduled posts file (`vk_scheduled_posts.json`) – array of objects:
[
  {
    "message": "Don't miss our latest update!",
    "attachments": [],
    "scheduled_at": "2025-03-01T12:00:00Z"
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
BOT_ID = "vk_community_bot"
BOT_NAME = "VKontakte Community Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "vk_community_config.json"
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
        return {
            "posted_hashes": [],
            "replied_comment_ids": [],
            "last_reply_time": 0
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── VK API helpers ───────────────────────────────────────────────
VK_API = "https://api.vk.com/method"

def vk_api_call(method: str, access_token: str, version: str, params: dict = None) -> Optional[dict]:
    """Make a call to VK API. Returns response dict or None on failure."""
    url = f"{VK_API}/{method}"
    payload = {
        "access_token": access_token,
        "v": version
    }
    if params:
        payload.update(params)
    try:
        resp = requests.post(url, data=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if "error" in data:
                _post(f"VK API error on {method}: {data['error']}", "error")
                return None
            return data.get("response")
        else:
            _post(f"VK API HTTP {resp.status_code} on {method}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"VK API request error: {e}", "error")
        return None

# ── Publishing ──────────────────────────────────────────────────
def post_to_wall(access_token: str, group_id: int, message: str,
                 attachments: list = None, version: str = "5.199") -> bool:
    """Post a message to the community wall. Returns True on success."""
    owner_id = -group_id
    params = {
        "owner_id": owner_id,
        "message": message,
        "from_group": 1
    }
    if attachments:
        params["attachments"] = ",".join(attachments)
    response = vk_api_call("wall.post", access_token, version, params)
    return response is not None

def process_scheduled_posts(config: dict, state: dict):
    """Publish posts that are due."""
    if not config.get("publishing", {}).get("enabled"):
        return
    file_path = config["publishing"].get("scheduled_posts_file", "vk_scheduled_posts.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            posts = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled posts: {e}", "error")
        return

    if not posts:
        return

    access_token = config["vk"]["access_token"]
    group_id = config["vk"]["group_id"]
    version = config["vk"].get("api_version", "5.199")
    now = datetime.now(timezone.utc)
    remaining = []
    posted_hashes = set(state.get("posted_hashes", []))

    for post in posts:
        scheduled_at_str = post.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(post)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(post)
            continue

        item_hash = str(hash(json.dumps(post, sort_keys=True)))
        if item_hash in posted_hashes:
            continue  # already posted

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            message = post.get("message", "")
            attachments = post.get("attachments", [])
            if not message and not attachments:
                _post("Skipping empty scheduled post", "warning")
                continue
            success = post_to_wall(access_token, group_id, message, attachments, version)
            if success:
                _post(f"Wall post published", "info")
                posted_hashes.add(item_hash)
                # success: remove from queue
                continue
            else:
                _post("Failed to publish wall post", "error")
        remaining.append(post)

    state["posted_hashes"] = list(posted_hashes)[-500:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Auto‑reply to comments ──────────────────────────────────────
def get_recent_wall_posts(access_token: str, group_id: int, count: int = 10,
                          version: str = "5.199") -> List[int]:
    """Return IDs of the most recent wall posts."""
    owner_id = -group_id
    params = {"owner_id": owner_id, "count": count, "filter": "owner"}
    response = vk_api_call("wall.get", access_token, version, params)
    if response and "items" in response:
        return [item["id"] for item in response["items"]]
    return []

def get_comments(access_token: str, group_id: int, post_id: int,
                 count: int = 20, version: str = "5.199") -> List[dict]:
    """Return comments for a specific wall post."""
    owner_id = -group_id
    params = {
        "owner_id": owner_id,
        "post_id": post_id,
        "count": count,
        "sort": "desc",
        "need_likes": 0
    }
    response = vk_api_call("wall.getComments", access_token, version, params)
    if response and "items" in response:
        return response["items"]
    return []

def reply_to_comment(access_token: str, group_id: int, post_id: int,
                     comment_id: int, message: str, version: str = "5.199") -> bool:
    """Reply to a comment. Returns True on success."""
    owner_id = -group_id
    params = {
        "owner_id": owner_id,
        "post_id": post_id,
        "reply_to_comment": comment_id,
        "message": message
    }
    response = vk_api_call("wall.createComment", access_token, version, params)
    return response is not None

def process_auto_reply(config: dict, state: dict):
    """Check recent posts for new comments and reply."""
    if not config.get("auto_reply", {}).get("enabled"):
        return

    access_token = config["vk"]["access_token"]
    group_id = config["vk"]["group_id"]
    version = config["vk"].get("api_version", "5.199")
    reply_template = config["auto_reply"].get("reply_template", "Thanks!")
    max_replies = int(config["auto_reply"].get("max_replies_per_run", 5))
    cooldown_minutes = int(config["auto_reply"].get("cooldown_minutes", 60))

    # Cooldown check
    now = time.time()
    last = state.get("last_reply_time", 0)
    if now - last < cooldown_minutes * 60:
        return  # too soon

    replied_ids = set(state.get("replied_comment_ids", []))
    new_replies = 0

    post_ids = get_recent_wall_posts(access_token, group_id, count=5, version=version)
    for post_id in post_ids:
        comments = get_comments(access_token, group_id, post_id, count=10, version=version)
        for comment in comments:
            comment_id = comment["id"]
            if comment_id in replied_ids:
                continue
            # Don't reply to own comments (from_group=1)
            if comment.get("from_id", 0) == -group_id:
                continue
            reply_text = reply_template
            success = reply_to_comment(access_token, group_id, post_id, comment_id, reply_text, version)
            if success:
                _post(f"Replied to comment {comment_id} on post {post_id}", "info")
                replied_ids.add(comment_id)
                new_replies += 1
                state["replied_comment_ids"] = list(replied_ids)[-500:]
                if new_replies >= max_replies:
                    break
            else:
                _post(f"Failed to reply to comment {comment_id}", "error")
            time.sleep(0.5)  # respect rate limits
        if new_replies >= max_replies:
            break

    if new_replies > 0:
        state["last_reply_time"] = now

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("VKontakte Community Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        access_token = config.get("vk", {}).get("access_token")
        if not access_token:
            _post("VK access token missing", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "vk_community_state.json")
        state = load_state(state_file)

        # Scheduled posts
        process_scheduled_posts(config, state)

        # Auto‑reply to comments
        process_auto_reply(config, state)

        save_state(state_file, state)

        poll_minutes = int(config.get("poll_interval_minutes", 15))
        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
