#!/usr/bin/env python3
"""
gmb_posting_bot.py — GMB (Google My Business) Posting Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Creates regular Google posts, events, and offers to keep
the business profile active and high‑ranking in local
search. Uses the Google Business Profile API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests

Configuration
─────────────
Place `gmb_posting_config.json` in the same directory:

{
  "google": {
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "refresh_token": "YOUR_REFRESH_TOKEN",
    "location_id": "locations/12345678901234567890",
    "account_id": "accounts/12345678901234567890"
  },
  "scheduled_posts_file": "gmb_scheduled_posts.json",
  "state_file": "gmb_posting_state.json",
  "heartbeat_interval": 30,
  "poll_interval_seconds": 30
}

Scheduled posts file (`gmb_scheduled_posts.json`) – array of objects:
[
  {
    "type": "standard",               // "standard", "event", or "offer"
    "summary": "Check out our new collection!",
    "body": "We've just released a brand new line of...",
    "image_url": "https://example.com/image.jpg",
    "action_url": "https://your-site.com",
    "topic_type": "STANDARD",
    "scheduled_at": "2025-06-10T09:00:00Z"
  },
  {
    "type": "event",
    "title": "Summer Sale Kickoff",
    "schedule": {
      "start_date": {"year": 2025, "month": 7, "day": 1},
      "start_time": {"hours": 10, "minutes": 0},
      "end_date": {"year": 2025, "month": 7, "day": 3},
      "end_time": {"hours": 18, "minutes": 0}
    }
  }
]

The API supports a subset of fields; refer to
https://developers.google.com/my-business/content/posts
for the exact structure.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "gmb_posting_bot"
BOT_NAME = "GMB Posting"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "gmb_posting_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(
            f"{HUB}/ingest",
            json={
                "bot_id": BOT_ID,
                "bot_name": BOT_NAME,
                "summary": summary,
                "level": level,
                "payload": payload or {},
            },
            timeout=5,
        )
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(
            f"{HUB}/heartbeat/{BOT_ID}",
            json={"bot_name": BOT_NAME, "status": "online"},
            timeout=3,
        )
    except Exception:
        pass
    _last_hb = time.time()

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"posted_hashes": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Google Business Profile service ──────────────────────────────
def get_gbp_service(config: dict):
    """Build and return an authenticated My Business API service v4."""
    google_cfg = config["google"]
    creds = Credentials(
        token=None,
        refresh_token=google_cfg["refresh_token"],
        token_uri="https://accounts.google.com/o/oauth2/token",
        client_id=google_cfg["client_id"],
        client_secret=google_cfg["client_secret"]
    )
    # The My Business API v4 uses the service name "mybusinessaccountmanagement" for account info,
    # but for posts we need the actual service "mybusinessbusinessinformation" or "mybusiness" depending.
    # We'll use "mybusiness" which includes localPosts.
    return build("mybusiness", "v4", credentials=creds)

# ── Create post ─────────────────────────────────────────────────
def create_gmb_post(service, account_id: str, location_id: str, post_data: dict) -> bool:
    """
    Create a local post on the given location.
    post_data is the JSON body for the post resource (see Google API docs).
    Returns True on success.
    """
    try:
        # The endpoint: accounts/{accountId}/locations/{locationId}/localPosts
        parent = f"{account_id}/{location_id}"
        request = service.accounts().locations().localPosts().create(parent=parent, body=post_data)
        response = request.execute()
        # If no error, it worked
        return True
    except Exception as e:
        _post(f"Failed to create Google post: {e}", "error")
        return False

# ── Scheduled posts processing ──────────────────────────────────
def process_scheduled_posts(config: dict, state: dict):
    file_path = config.get("scheduled_posts_file", "gmb_scheduled_posts.json")
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

    service = get_gbp_service(config)
    account_id = config["google"]["account_id"]
    location_id = config["google"]["location_id"]
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
            continue

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            # Build the post body based on type
            post_type = post.get("type", "standard")
            body = {
                "topicType": post.get("topic_type", "STANDARD"),
                "summary": post.get("summary", ""),
                "callToAction": {
                    "actionType": post.get("action_type", "LEARN_MORE"),
                    "url": post.get("action_url", "")
                }
            }
            # For media, add if provided
            if "image_url" in post and post["image_url"]:
                body["media"] = [
                    {
                        "mediaFormat": "PHOTO",
                        "sourceUrl": post["image_url"]
                    }
                ]
            # Event and offer specific fields
            if post_type == "event":
                body["event"] = {
                    "title": post.get("title", ""),
                    "schedule": post.get("schedule", {})
                }
            elif post_type == "offer":
                body["offer"] = {
                    "title": post.get("title", ""),
                    "couponCode": post.get("coupon_code", ""),
                    "redeemOnlineUrl": post.get("redeem_online_url", ""),
                    "termsConditions": post.get("terms", "")
                }
            # Create the post
            success = create_gmb_post(service, account_id, location_id, body)
            if success:
                _post(f"GMB post created: {post.get('summary','')[:50]}", "info")
                posted_hashes.add(item_hash)
                # success: remove from queue
                continue
            else:
                _post("Failed to create GMB post, keeping for retry", "error")
        remaining.append(post)

    state["posted_hashes"] = list(posted_hashes)[-500:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("GMB Posting Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "gmb_posting_state.json")
        state = load_state(state_file)

        # Process scheduled posts every 30 seconds
        while True:
            process_scheduled_posts(config, state)
            save_state(state_file, state)
            _heartbeat()
            time.sleep(int(config.get("poll_interval_seconds", 30)))

if __name__ == "__main__":
    main()
