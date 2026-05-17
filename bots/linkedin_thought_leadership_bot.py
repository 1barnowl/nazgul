#!/usr/bin/env python3
"""
linkedin_thought_leadership_bot.py — LinkedIn Thought Leadership Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Publishes long‑form posts and collaborative articles via the
LinkedIn API.  Leaves professional comments on industry leader
posts.  Sends connection requests with personalised notes (not
currently supported by the public API; this function is disabled).

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `linkedin_thought_config.json` in the same directory:

{
  "linkedin": {
    "access_token": "AQV...",
    "profile_urn": "urn:li:person:123456789"     // your LinkedIn person URN
  },
  "publishing": {
    "enabled": true,
    "content_file": "linkedin_posts.json"
  },
  "commenting": {
    "enabled": true,
    "target_posts": [
      "urn:li:activity:1234567890",
      "urn:li:share:9876543210"
    ],
    "comment_template": "Thanks for sharing this valuable insight, {author}! I've been experimenting with similar approaches and would love to connect.",
    "author_names": ["John Doe", "Jane Smith"]   // match indices with target_posts
  },
  "state_file": "linkedin_thought_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 60
}

Content file (`linkedin_posts.json`) format:
[
  {
    "text": "Today's thought on AI...",
    "url": null,                           // optional link to share
    "title": "Optional link title",
    "scheduled_at": "2025-01-20T10:00:00Z"
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
BOT_ID   = "linkedin_thought_leadership_bot"
BOT_NAME = "LinkedIn Thought Leadership"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "linkedin_thought_config.json"
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
        return {"published_ids": [], "commented_posts": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── LinkedIn API helpers ─────────────────────────────────────────
LINKEDIN_API = "https://api.linkedin.com/v2"

def linkedin_post(endpoint: str, body: dict, headers: dict) -> Optional[dict]:
    url = f"{LINKEDIN_API}/{endpoint}"
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            _post(f"LinkedIn API error on POST {endpoint}: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"LinkedIn POST request error: {e}", "error")
        return None

def linkedin_get(endpoint: str, headers: dict) -> Optional[dict]:
    url = f"{LINKEDIN_API}/{endpoint}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"LinkedIn API error on GET {endpoint}: {resp.status_code}", "error")
            return None
    except Exception as e:
        _post(f"LinkedIn GET request error: {e}", "error")
        return None

def _auth_headers(access_token: str) -> dict:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json"
    }

# ── Posting (UGC Posts) ──────────────────────────────────────────
def create_share(access_token: str, profile_urn: str, text: str,
                 url: Optional[str] = None, title: Optional[str] = None) -> Optional[str]:
    """
    Create a text/URL share via the LinkedIn Share API.
    Returns the activity URN if successful.
    """
    headers = _auth_headers(access_token)
    body = {
        "author": profile_urn,
        "lifecycleState": "PUBLISHED",
        "specificContent": {
            "com.linkedin.ugc.ShareContent": {
                "shareCommentary": {"text": text},
                "shareMediaCategory": "NONE"
            }
        },
        "visibility": {
            "com.linkedin.ugc.MemberNetworkVisibility": "PUBLIC"
        }
    }
    if url:
        body["specificContent"]["com.linkedin.ugc.ShareContent"]["shareMediaCategory"] = "ARTICLE"
        body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"] = [{
            "status": "READY",
            "originalUrl": url,
            "title": title or ""
        }]
    resp = linkedin_post("ugcPosts", body, headers)
    if resp and "id" in resp:
        return resp["id"]
    return None

def process_content_file(config: dict, state: dict):
    """Read scheduled posts file and publish any that are due."""
    file_path = config.get("publishing", {}).get("content_file", "linkedin_posts.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            posts = json.load(f)
    except Exception as e:
        _post(f"Error reading content file: {e}", "error")
        return

    access_token = config["linkedin"]["access_token"]
    profile_urn = config["linkedin"]["profile_urn"]
    now = datetime.now(timezone.utc)
    remaining = []
    for post in posts:
        scheduled_at_str = post.get("scheduled_at")
        if scheduled_at_str:
            try:
                scheduled_dt = datetime.fromisoformat(scheduled_at_str)
            except ValueError:
                remaining.append(post)
                continue
            if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
                text = post.get("text", "")
                url = post.get("url")
                title = post.get("title")
                activity_urn = create_share(access_token, profile_urn, text, url, title)
                if activity_urn:
                    _post(f"Published post: {text[:80]}", "info", {"activity_urn": activity_urn})
                    # Do not keep in file (published successfully)
                    continue
                else:
                    _post("Failed to publish post, will retry", "error")
            # else keep for future
        remaining.append(post)
    # Write back remaining posts
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Commenting on industry leader posts ───────────────────────────
def comment_on_post(access_token: str, post_urn: str, message: str) -> bool:
    """Add a comment to a LinkedIn post (activity or share)."""
    # The API endpoint: /socialActions/{postUrn}/comments
    # postUrn should be URL-encoded. But we use the URN directly? LinkedIn expects encoded URN.
    # We'll URL-encode the colon characters: urn%3Ali%3A...
    import urllib.parse
    encoded_urn = urllib.parse.quote(post_urn, safe="")
    endpoint = f"socialActions/{encoded_urn}/comments"
    headers = _auth_headers(access_token)
    body = {
        "object": post_urn,
        "message": message
    }
    resp = linkedin_post(endpoint, body, headers)
    return resp is not None

def process_comments(config: dict, state: dict):
    """Add comments to pre‑configured target posts if not already done."""
    if not config.get("commenting", {}).get("enabled"):
        return
    access_token = config["linkedin"]["access_token"]
    target_posts = config["commenting"].get("target_posts", [])
    authors = config["commenting"].get("author_names", [])
    template = config["commenting"].get("comment_template",
        "Great insights! I've been exploring similar areas and would be happy to connect.")

    for idx, post_urn in enumerate(target_posts):
        if post_urn in state.get("commented_posts", []):
            continue
        # Build personalised comment
        author = authors[idx] if idx < len(authors) else ""
        comment = template.replace("{author}", author)
        success = comment_on_post(access_token, post_urn, comment)
        if success:
            _post(f"Commented on post {post_urn}", "info")
            state.setdefault("commented_posts", []).append(post_urn)
            time.sleep(1)  # rate limit
        else:
            _post(f"Failed to comment on {post_urn}", "error")

# ── Connection requests (not supported by the public API) ────────
def send_connection_request(access_token, target_urn, note):
    """This function is not implemented because LinkedIn's public API
    does not support sending connection invitations.  If you have a
    dedicated method (e.g., Sales Navigator API), you can extend here."""
    _post("Connection requests are not supported by the LinkedIn API; skipping.", "warning")
    return False

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("LinkedIn Thought Leadership Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        access_token = config.get("linkedin", {}).get("access_token")
        if not access_token:
            _post("Missing LinkedIn access token", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "linkedin_thought_state.json")
        state = load_state(state_file)

        # Scheduled posts
        process_content_file(config, state)

        # Comment on target posts
        process_comments(config, state)

        # Connection requests are disabled due to API limitations
        # (The function above exists for potential future use)

        save_state(state_file, state)

        poll_min = int(config.get("poll_interval_minutes", 60))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
