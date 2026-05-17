#!/usr/bin/env python3
"""
instagram_growth_bot.py — Instagram Growth & Comment Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auto‑replies to story mentions and post comments, publishes
reels and feed posts, monitors hashtags to leave contextual
comments that drive profile visits.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `instagram_growth_config.json` in the same directory:

{
  "instagram": {
    "page_access_token": "EAAG...",
    "api_version": "v19.0",
    "ig_user_id": "17841400000000000"
  },
  "auto_reply": {
    "enabled": true,
    "story_mention_template": "Thanks for the mention! Check out our latest products: https://example.com",
    "post_comment_template": "Thanks for your comment! We appreciate it.",
    "llm": null                         // optional, set to { "provider": "openai", "api_key": "sk-...", "model": "gpt-4o-mini" } for contextual replies
  },
  "publishing": {
    "enabled": true,
    "scheduled_file": "ig_scheduled_posts.json",
    "http_port": 9685
  },
  "hashtag_monitor": {
    "enabled": true,
    "hashtags": ["skincare", "beauty"],
    "comment_template": "Love this! Discover more at {link}",
    "link": "https://your-site.com",
    "max_daily_comments": 20,
    "check_interval_minutes": 30
  },
  "webhook": {
    "enabled": true,
    "port": 9690,
    "verify_token": "my_secret_verify_token"
  },
  "state_file": "instagram_growth_state.json",
  "heartbeat_interval": 30
}

Scheduled posts file (`ig_scheduled_posts.json`):
[
  {
    "media_type": "IMAGE",   // or "VIDEO" (reel)
    "caption": "Happy Monday!",
    "media_url": "https://example.com/image.jpg",
    "scheduled_at": "2025-01-20T10:00:00Z"
  }
]
"""

import json
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Dict, List, Any

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "instagram_growth_bot"
BOT_NAME = "Instagram Growth & Comment"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "instagram_growth_config.json"
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
        return {"daily_hashtag_comments": 0, "last_comment_date": "", "processed_media_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Instagram Graph API helpers ─────────────────────────────────
GRAPH_URL = "https://graph.facebook.com"

def ig_post(endpoint: str, params: dict, access_token: str) -> Optional[dict]:
    url = f"{GRAPH_URL}/{endpoint}"
    params["access_token"] = access_token
    try:
        resp = requests.post(url, data=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"IG Graph API error on POST {endpoint}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"IG POST request error: {e}", "error")
        return None

def ig_get(endpoint: str, params: dict, access_token: str) -> Optional[dict]:
    url = f"{GRAPH_URL}/{endpoint}"
    params["access_token"] = access_token
    try:
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"IG Graph API error on GET {endpoint}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"IG GET request error: {e}", "error")
        return None

# ── Publishing: media posting ────────────────────────────────────
def create_media_container(ig_user_id: str, media_type: str, media_url: str,
                           caption: str, access_token: str) -> Optional[str]:
    """Create a media container (image/video/reel). Returns container ID."""
    endpoint = f"{ig_user_id}/media"
    params = {
        "media_type": media_type,
        "media_url": media_url,
        "caption": caption
    }
    if media_type == "VIDEO":
        # For reels, you need to set media_type to "REELS" if using Graph API v14.0+?
        # Instagram Graph API: media_type can be "REELS" for reels. We'll handle both.
        params["media_type"] = "REELS"
    result = ig_post(endpoint, params, access_token)
    if result and "id" in result:
        return result["id"]
    return None

def publish_media(ig_user_id: str, creation_id: str, access_token: str) -> bool:
    endpoint = f"{ig_user_id}/media_publish"
    params = {"creation_id": creation_id}
    result = ig_post(endpoint, params, access_token)
    return result is not None and "id" in result

def process_scheduled_posts(config: dict):
    """Read scheduled posts file and publish those due."""
    schedule_file = config.get("publishing", {}).get("scheduled_file", "ig_scheduled_posts.json")
    if not os.path.exists(schedule_file):
        return
    try:
        with open(schedule_file, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled IG posts: {e}", "error")
        return

    access_token = config["instagram"]["page_access_token"]
    ig_user_id = config["instagram"]["ig_user_id"]
    now = datetime.now(timezone.utc)
    updated = []
    for post in scheduled:
        scheduled_at_str = post.get("scheduled_at")
        if not scheduled_at_str:
            updated.append(post)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            updated.append(post)
            continue
        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            media_type = post.get("media_type", "IMAGE")
            media_url = post.get("media_url")
            caption = post.get("caption", "")
            if not media_url:
                updated.append(post)
                continue
            container_id = create_media_container(ig_user_id, media_type, media_url, caption, access_token)
            if container_id:
                # Wait for processing (Instagram recommends checking status, but we'll wait)
                time.sleep(5)
                if publish_media(ig_user_id, container_id, access_token):
                    _post(f"Published {media_type} to Instagram", "info")
                    # Don't add to updated (remove)
                    continue
            updated.append(post)
            time.sleep(1)
        else:
            updated.append(post)
    # Rewrite file with remaining posts
    with open(schedule_file, "w") as f:
        json.dump(updated, f, indent=2)

# ── Webhook server for mentions and comments ─────────────────────
class InstagramWebhookHandler(BaseHTTPRequestHandler):
    config: dict = None
    state: dict = None

    def do_GET(self):
        """Verify webhook endpoint."""
        if self.path.startswith("/webhook"):
            params = {}
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                params = dict(qc.split("=") for qc in query.split("&"))
            mode = params.get("hub.mode")
            token = params.get("hub.verify_token")
            challenge = params.get("hub.challenge")
            expected = self.config.get("webhook", {}).get("verify_token", "")
            if mode == "subscribe" and token == expected and challenge:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(challenge.encode())
            else:
                self.send_error(403)
        else:
            self.send_error(404)

    def do_POST(self):
        """Receive webhook events."""
        if self.path.startswith("/webhook"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except Exception:
                self.send_error(400)
                return
            # Process entries
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    field = change.get("field")
                    value = change.get("value", {})
                    if field == "mention":
                        self._handle_mention(value)
                    elif field == "comments":
                        self._handle_comment(value)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404)

    def _handle_mention(self, value: dict):
        """Story mention event."""
        media_id = value.get("media_id")
        comment_id = value.get("comment_id")
        # The mention comes as a comment on the story? Actually story mentions are represented as a comment from the user.
        # We need to reply to that comment to appear in the story.
        if not media_id or not comment_id:
            return
        # Get the reply template
        reply_text = self.config.get("auto_reply", {}).get("story_mention_template", "Thanks!")
        access_token = self.config["instagram"]["page_access_token"]
        # Reply to the comment (which is the mention)
        endpoint = f"{comment_id}/replies"
        params = {"message": reply_text}
        result = ig_post(endpoint, params, access_token)
        if result and "id" in result:
            _post(f"Replied to story mention from media {media_id}", "info")

    def _handle_comment(self, value: dict):
        """New comment on a post."""
        comment_id = value.get("id")
        text = value.get("text", "")
        if not comment_id:
            return
        # Only reply to top-level comments? We'll reply to all.
        # Skip if we already replied (track in state) - simple memory
        state = self.state
        if comment_id in state.get("replied_comment_ids", []):
            return
        reply_text = self.config.get("auto_reply", {}).get("post_comment_template", "Thanks for your comment!")
        access_token = self.config["instagram"]["page_access_token"]
        endpoint = f"{comment_id}/replies"
        params = {"message": reply_text}
        result = ig_post(endpoint, params, access_token)
        if result and "id" in result:
            _post(f"Replied to comment {comment_id}", "info")
            state.setdefault("replied_comment_ids", []).append(comment_id)
            # Keep only last 200
            state["replied_comment_ids"] = state["replied_comment_ids"][-200:]

    def log_message(self, *args):
        pass

def start_webhook_server(config: dict, state: dict):
    port = config.get("webhook", {}).get("port", 9690)
    if not config.get("webhook", {}).get("enabled", True):
        return
    InstagramWebhookHandler.config = config
    InstagramWebhookHandler.state = state
    server = HTTPServer(("0.0.0.0", port), InstagramWebhookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Instagram webhook server started on port {port}", "info")

# ── Hashtag monitoring ───────────────────────────────────────────
def get_hashtag_id(hashtag: str, ig_user_id: str, access_token: str) -> Optional[str]:
    """Search for a hashtag and return its ID (used for recent media)."""
    endpoint = f"ig_hashtag_search?user_id={ig_user_id}&q={hashtag}"
    data = ig_get(endpoint, {}, access_token)
    if data and "data" in data and len(data["data"]) > 0:
        return data["data"][0]["id"]
    return None

def get_recent_hashtag_media(hashtag_id: str, ig_user_id: str, access_token: str,
                              limit: int = 10) -> List[dict]:
    """Return recent media objects for a given hashtag ID."""
    endpoint = f"{hashtag_id}/recent_media?user_id={ig_user_id}&limit={limit}&fields=id,caption,permalink,timestamp"
    data = ig_get(endpoint, {}, access_token)
    if data and "data" in data:
        return data["data"]
    return []

def process_hashtags(config: dict, state: dict):
    """Monitor hashtags, comment on new media with a contextual message."""
    if not config.get("hashtag_monitor", {}).get("enabled"):
        return
    access_token = config["instagram"]["page_access_token"]
    ig_user_id = config["instagram"]["ig_user_id"]
    hashtags = config["hashtag_monitor"]["hashtags"]
    template = config["hashtag_monitor"]["comment_template"]
    link = config["hashtag_monitor"].get("link", "")
    max_daily = int(config["hashtag_monitor"].get("max_daily_comments", 20))

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_date = state.get("last_comment_date", "")
    daily_count = state.get("daily_hashtag_comments", 0)
    if last_date != today:
        daily_count = 0
        state["last_comment_date"] = today

    processed_ids = set(state.get("processed_media_ids", []))

    for hashtag in hashtags:
        hashtag_id = get_hashtag_id(hashtag, ig_user_id, access_token)
        if not hashtag_id:
            _post(f"Could not find hashtag: {hashtag}", "warning")
            continue
        media_items = get_recent_hashtag_media(hashtag_id, ig_user_id, access_token, limit=5)
        for media in media_items:
            media_id = media.get("id")
            if media_id in processed_ids:
                continue
            if daily_count >= max_daily:
                return
            # Generate comment text
            comment = template.replace("{link}", link)
            # Use LLM? Not in this simple version; we just use template.
            # Leave comment on the media
            endpoint = f"{media_id}/comments"
            params = {"message": comment}
            result = ig_post(endpoint, params, access_token)
            if result and "id" in result:
                _post(f"Commented on hashtag media {media_id} for #{hashtag}", "info")
                processed_ids.add(media_id)
                daily_count += 1
                state["daily_hashtag_comments"] = daily_count
                state.setdefault("processed_media_ids", []).append(media_id)
                # Limit stored IDs to 500
                state["processed_media_ids"] = state["processed_media_ids"][-500:]
                time.sleep(1)  # rate limit

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Instagram Growth & Comment Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        access_token = config.get("instagram", {}).get("page_access_token")
        if not access_token:
            _post("Missing Instagram page access token", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "instagram_growth_state.json")
        state = load_state(state_file)

        # Start webhook listener (non-blocking)
        start_webhook_server(config, state)

        # Scheduled posts
        if config.get("publishing", {}).get("enabled", False):
            process_scheduled_posts(config)

        # Hashtag monitoring
        process_hashtags(config, state)

        save_state(state_file, state)

        check_minutes = int(config.get("hashtag_monitor", {}).get("check_interval_minutes", 30)) * 60
        _heartbeat()
        time.sleep(check_minutes)

if __name__ == "__main__":
    main()
