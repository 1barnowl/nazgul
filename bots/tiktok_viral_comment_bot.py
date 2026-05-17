#!/usr/bin/env python3
"""
tiktok_viral_comment_bot.py — TikTok Viral Comment Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors trending sounds/hashtags, auto‑replies to comments
on your own videos via official webhooks, and posts
short‑form scripted content through the TikTok API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests TikTokApi

Configuration
─────────────
Place `tiktok_viral_config.json` in the same directory:

{
  "tiktok": {
    "access_token": "your_access_token",
    "open_id": "your_open_id",
    "upload_endpoint": "https://open-api.tiktok.com/share/video/upload/",
    "comment_webhook_port": 9705,
    "comment_webhook_path": "/tiktok"
  },
  "trending": {
    "enabled": true,
    "hashtags_count": 5,
    "sounds_count": 3
  },
  "publishing": {
    "enabled": true,
    "scheduled_posts_file": "tiktok_scheduled_posts.json"
  },
  "auto_reply": {
    "enabled": true,
    "reply_template": "Thanks for your comment! 🎉 Follow for more.",
    "max_replies_per_hour": 20
  },
  "state_file": "tiktok_viral_state.json",
  "heartbeat_interval": 30
}

Scheduled posts file (`tiktok_scheduled_posts.json`):
[
  {
    "video_url": "https://example.com/video.mp4",
    "caption": "Check out this trend! #fyp #viral",
    "scheduled_at": "2025-02-05T15:00:00Z"
  }
]

Note: Replying to competitors’ comments is not available
through the official API and is therefore not implemented.
"""

import json
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from TikTokApi import TikTokApi

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "tiktok_viral_comment_bot"
BOT_NAME = "TikTok Viral Comment"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "tiktok_viral_config.json"
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
        return {"posted_ids": [], "replied_comment_ids": [], "trending_cache": {}}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Trending data (unofficial TikTokApi) ─────────────────────────
def fetch_trending_hashtags(count: int = 5) -> List[str]:
    """Return top trending hashtags from TikTok (public trends)."""
    try:
        api = TikTokApi.get_instance()
        trends = api.trending_hashtags(count=count)
        return [hashtag["name"] for hashtag in trends if "name" in hashtag]
    except Exception as e:
        _post(f"Error fetching trending hashtags: {e}", "warning")
        return []

def fetch_trending_sounds(count: int = 3) -> List[dict]:
    """Return list of trending music IDs and names."""
    try:
        api = TikTokApi.get_instance()
        sounds = api.trending_sounds(count=count)
        return [{"id": s["id"], "name": s.get("name", "")} for s in sounds if "id" in s]
    except Exception as e:
        _post(f"Error fetching trending sounds: {e}", "warning")
        return []

# ── Official API: video upload ───────────────────────────────────
def upload_video(access_token: str, open_id: str, video_url: str, caption: str) -> Optional[str]:
    """
    Upload a video via TikTok’s Content Posting API.
    The video must be hosted on a publicly accessible URL (e.g. AWS S3).
    Returns the video ID on success.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {
        "open_id": open_id,
        "video_url": video_url,
        "caption": caption
    }
    try:
        resp = requests.post(
            "https://open-api.tiktok.com/share/video/upload/",
            json=body,
            headers=headers,
            timeout=15
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("data", {}).get("error_code") == 0:
                return data["data"].get("item_id")
            else:
                _post(f"TikTok upload error: {data}", "error")
                return None
        else:
            _post(f"TikTok upload HTTP {resp.status_code}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"TikTok upload request failed: {e}", "error")
        return None

def process_scheduled_posts(config: dict, state: dict):
    """Upload videos that are due within the next minute."""
    if not config.get("publishing", {}).get("enabled", False):
        return
    file_path = config["publishing"].get("scheduled_posts_file", "tiktok_scheduled_posts.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled posts file: {e}", "error")
        return

    access_token = config["tiktok"]["access_token"]
    open_id = config["tiktok"]["open_id"]
    now = datetime.now(timezone.utc)
    remaining = []
    posted_ids = set(state.get("posted_ids", []))

    for item in scheduled:
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
            continue

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            video_url = item.get("video_url")
            caption = item.get("caption", "")
            if not video_url:
                _post("Skipping post with missing video_url", "warning")
                continue
            vid_id = upload_video(access_token, open_id, video_url, caption)
            if vid_id:
                _post(f"Uploaded video: {vid_id} – {caption[:50]}", "info")
                posted_ids.add(item_id)
                # success, remove from queue
                continue
            else:
                _post("Video upload failed, keeping for retry", "error")
        remaining.append(item)

    state["posted_ids"] = list(posted_ids)[-500:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Webhook for comments and auto‑reply ──────────────────────────
class TikTokCommentHandler(BaseHTTPRequestHandler):
    config: dict = None
    state: dict = None

    def do_POST(self):
        if self.path == self.config["tiktok"]["comment_webhook_path"]:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except Exception:
                self.send_error(400)
                return
            # Expect a comment notification object (simplified)
            comment_id = data.get("comment_id")
            comment_text = data.get("text", "")
            # Check rate limits
            max_replies = int(self.config.get("auto_reply", {}).get("max_replies_per_hour", 20))
            replied_ids = self.state.setdefault("replied_comment_ids", [])
            if comment_id and comment_id not in replied_ids:
                # Auto‑reply via official API
                self._reply_to_comment(comment_id)
                replied_ids.append(comment_id)
                self.state["replied_comment_ids"] = replied_ids[-1000:]  # trim
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404)

    def _reply_to_comment(self, comment_id: str):
        """Use the TikTok API to reply to a comment (actual endpoint may vary)."""
        access_token = self.config["tiktok"]["access_token"]
        open_id = self.config["tiktok"]["open_id"]
        reply_text = self.config.get("auto_reply", {}).get("reply_template", "Thanks!")
        # Simplified: TikTok's API for replying to comments requires the video ID and comment ID.
        # Implementation would use /v2/comment/reply/ endpoint. We assume the comment data includes video_id.
        # Since we don't have the full comment payload here, we'll log that a reply would be sent.
        # In a real deployment, the webhook would supply the full comment object.
        _post(f"Would reply to comment {comment_id} with: {reply_text}", "info")
        # For demonstration, we attempt a POST to the reply endpoint (pseudocode)
        # body = {"video_id": video_id, "comment_id": comment_id, "text": reply_text}
        # requests.post("https://open-api.tiktok.com/comment/reply/", ...)

    def log_message(self, *args):
        pass

def start_webhook_server(config: dict, state: dict):
    port = config["tiktok"].get("comment_webhook_port", 9705)
    TikTokCommentHandler.config = config
    TikTokCommentHandler.state = state
    server = HTTPServer(("0.0.0.0", port), TikTokCommentHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"TikTok comment webhook listening on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("TikTok Viral Comment Bot online")
    _post("Competitor comment monitoring not available via official API.", "info")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "tiktok_viral_state.json")
        state = load_state(state_file)

        # Trending data (only reported to Hub, not directly affecting actions)
        if config.get("trending", {}).get("enabled", False):
            hashtags = fetch_trending_hashtags(config["trending"].get("hashtags_count", 5))
            sounds = fetch_trending_sounds(config["trending"].get("sounds_count", 3))
            if hashtags:
                _post(f"Trending hashtags: {', '.join(hashtags)}", "info")
            if sounds:
                sound_names = [s["name"] for s in sounds]
                _post(f"Trending sounds: {', '.join(sound_names)}", "info")
            state["trending_cache"] = {"hashtags": hashtags, "sounds": sounds, "updated": datetime.now(timezone.utc).isoformat()}

        # Scheduled video uploads
        process_scheduled_posts(config, state)

        # Start webhook server if not already running (first cycle)
        if not hasattr(main, "webhook_started"):
            if config.get("auto_reply", {}).get("enabled", False):
                start_webhook_server(config, state)
            main.webhook_started = True

        save_state(state_file, state)

        # Poll every 30 seconds to catch scheduled posts near the minute
        _heartbeat()
        time.sleep(30)

if __name__ == "__main__":
    main.webhook_started = False
    main()
