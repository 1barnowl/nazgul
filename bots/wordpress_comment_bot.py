#!/usr/bin/env python3
"""
wordpress_comment_bot.py — WordPress / Blog Comment Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Finds compatible blog posts via the WordPress REST API,
leaves well‑crafted comments that pass moderation, and
includes a soft link back to your site.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `wordpress_comment_config.json` in the same directory:

{
  "targets": [
    {
      "name": "Example Blog",
      "base_url": "https://example.com",
      "username": null,           // optional; for authenticated commenting
      "password": null            // application password or regular password
    }
  ],
  "search": {
    "keywords": ["marketing automation", "web scraping"],
    "post_age_days": 7,          // only posts newer than this
    "max_posts_per_site": 5
  },
  "comment": {
    "author_name": "John from ExampleCo",
    "author_email": "john@example.com",
    "author_url": "https://example.com",
    "comment_template": "Great post! I've been exploring similar ideas and wrote a guide on this topic: https://example.com/guide",
    "llm": null                  // optional OpenAI config for generating custom comments
  },
  "rate_limit": {
    "cooldown_seconds": 300,
    "max_comments_per_run": 3
  },
  "state_file": "wordpress_comment_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 60
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
BOT_ID = "wordpress_comment_bot"
BOT_NAME = "WordPress / Blog Comment"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "wordpress_comment_config.json"
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
        return {"commented_post_ids": {}, "last_comment_time": 0}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── WordPress API helpers ────────────────────────────────────────
def wp_get_posts(base_url: str, params: dict, auth: Optional[tuple] = None) -> List[dict]:
    """Fetch recent posts matching keywords."""
    url = f"{base_url.rstrip('/')}/wp-json/wp/v2/posts"
    try:
        resp = requests.get(url, params=params, auth=auth, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"WP API error on {base_url}: {resp.status_code} {resp.text[:200]}", "error")
            return []
    except Exception as e:
        _post(f"WP request failed for {base_url}: {e}", "error")
        return []

def wp_post_comment(base_url: str, post_id: int, comment_data: dict,
                    auth: Optional[tuple] = None) -> Optional[int]:
    """Post a comment on a specific post. Returns comment ID if successful."""
    url = f"{base_url.rstrip('/')}/wp-json/wp/v2/comments"
    params = {
        "post": post_id,
        "content": comment_data["content"],
        "author_name": comment_data["author_name"],
        "author_email": comment_data["author_email"],
        "author_url": comment_data.get("author_url", "")
    }
    # If auth is provided, the user is authenticated; otherwise, comment may be posted as anonymous,
    # but many sites require authentication. We'll send the comment with auth if available.
    try:
        resp = requests.post(url, json=params, auth=auth, timeout=15)
        if resp.status_code == 201:
            return resp.json()["id"]
        elif resp.status_code == 401 and auth is None:
            # Try with auth if missing but optional? Not now.
            _post(f"Comment posting not allowed on {base_url} (authentication required)", "warning")
            return None
        else:
            _post(f"Comment post error on {base_url}: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Comment post request failed: {e}", "error")
        return None

# ── Main logic ───────────────────────────────────────────────────
def process_targets(config: dict, state: dict):
    """Iterate over configured WordPress sites, find new posts, and comment."""
    targets = config.get("targets", [])
    search_cfg = config.get("search", {})
    comment_cfg = config.get("comment", {})
    rate_cfg = config.get("rate_limit", {})

    keywords = search_cfg.get("keywords", [])
    max_age_days = int(search_cfg.get("post_age_days", 7))
    max_posts_per_site = int(search_cfg.get("max_posts_per_site", 5))
    cooldown = float(rate_cfg.get("cooldown_seconds", 300))
    max_comments_per_run = int(rate_cfg.get("max_comments_per_run", 3))

    commented_ids = state.setdefault("commented_post_ids", {})
    last_comment_time = state.get("last_comment_time", 0)
    now = time.time()

    # Cool-down check
    if now - last_comment_time < cooldown:
        return

    comments_posted = 0

    for target in targets:
        base_url = target["base_url"].rstrip("/")
        username = target.get("username")
        password = target.get("password")
        auth = (username, password) if username and password else None

        # Build search query: WordPress REST API supports 'search' parameter
        # We'll search for each keyword
        for keyword in keywords:
            if comments_posted >= max_comments_per_run:
                break
            # Fetch recent posts matching the keyword
            # We'll also filter by date using 'after' and 'before' parameters (ISO 8601)
            date_after = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat()
            params = {
                "search": keyword,
                "per_page": max_posts_per_site,
                "status": "publish",
                "orderby": "date",
                "order": "desc",
                "after": date_after
            }
            posts = wp_get_posts(base_url, params, auth)
            for post in posts:
                if comments_posted >= max_comments_per_run:
                    break
                post_id = post["id"]
                # Check if we already commented on this post (tracked globally with site prefix)
                unique_key = f"{base_url}:{post_id}"
                if unique_key in commented_ids:
                    continue
                # Ensure comments are open
                if post.get("comment_status") != "open":
                    continue
                # Prepare comment text
                comment_text = comment_cfg.get("comment_template", "")
                # Optionally generate with LLM
                llm_cfg = comment_cfg.get("llm")
                if llm_cfg and llm_cfg.get("api_key"):
                    # We could use OpenAI to generate a custom comment based on post title, but we'll skip to keep dependencies light
                    pass
                # Replace any placeholders (we could add {post_title}, etc.)
                comment_text = comment_text.replace("{post_title}", post.get("title", {}).get("rendered", ""))
                comment_data = {
                    "content": comment_text,
                    "author_name": comment_cfg.get("author_name", "Anonymous"),
                    "author_email": comment_cfg.get("author_email", "no-reply@example.com"),
                    "author_url": comment_cfg.get("author_url", "")
                }
                comment_id = wp_post_comment(base_url, post_id, comment_data, auth)
                if comment_id:
                    _post(f"Commented on {base_url}/?p={post_id} (post ID {post_id})", "info")
                    commented_ids[unique_key] = True
                    comments_posted += 1
                    state["last_comment_time"] = now
                    # Trim commented IDs if necessary (keep last 500)
                    if len(commented_ids) > 500:
                        # Just keep the latest 500 entries (convert to list and truncate)
                        # Since commented_ids is a dict, we'll just allow it to grow for now
                        pass
                    time.sleep(1)  # be polite
                else:
                    _post(f"Failed to comment on post {post_id} at {base_url}", "error")

            # Slight delay between sites to avoid hammering
            time.sleep(1)

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("WordPress / Blog Comment Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "wordpress_comment_state.json")
        state = load_state(state_file)

        process_targets(config, state)

        save_state(state_file, state)

        poll_min = int(config.get("poll_interval_minutes", 60))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
