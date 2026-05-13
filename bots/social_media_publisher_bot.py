#!/usr/bin/env python3
"""
social_media_publisher_bot.py — Social Media Publisher Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Posts scheduled content to LinkedIn, X (Twitter), Facebook,
and TikTok via their official APIs. Triggers are read from a
schedule file, and an HTTP endpoint allows on‑demand posting.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests requests-oauthlib schedule

Configuration
─────────────
Place `social_publisher_config.json` in the same directory:

{
  "linkedin": {
    "access_token": "your_linkedin_access_token",
    "urn": "urn:li:organization:123456"
  },
  "twitter": {
    "api_key": "your_api_key",
    "api_secret": "your_api_secret",
    "access_token": "your_access_token",
    "access_token_secret": "your_access_token_secret"
  },
  "facebook": {
    "page_id": "your_page_id",
    "access_token": "your_page_access_token"
  },
  "tiktok": {
    "access_token": "your_tiktok_access_token",
    "open_id": "your_tiktok_open_id"
  },
  "schedule_file": "scheduled_posts.json",
  "http_port": 9610,
  "poll_interval_seconds": 60,
  "heartbeat_interval": 30
}

Scheduled posts file format (scheduled_posts.json):
[
  {
    "platform": "twitter",
    "text": "Hello world from the bot!",
    "scheduled_at": "2025-03-20T15:00:00Z",
    "media_urls": [],
    "link": null
  },
  ...
]
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional

import requests
from schedule import Scheduler

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "social_media_publisher_bot"
BOT_NAME = "Social Media Publisher"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "social_publisher_config.json"
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

# ── Provider interfaces ──────────────────────────────────────────

class LinkedInProvider:
    def __init__(self, access_token: str, urn: str):
        self.access_token = access_token
        self.urn = urn

    def post(self, text: str, media_urls: List[str] = None, link: str = None) -> dict:
        url = "https://api.linkedin.com/v2/ugcPosts"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "Content-Type": "application/json"
        }
        body = {
            "author": self.urn,
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
        if link:
            body["specificContent"]["com.linkedin.ugc.ShareContent"]["shareMediaCategory"] = "ARTICLE"
            body["specificContent"]["com.linkedin.ugc.ShareContent"]["media"] = [{
                "status": "READY",
                "originalUrl": link
            }]
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=10)
            if resp.status_code in (200, 201):
                return {"success": True, "provider": "linkedin", "post_id": resp.json().get("id")}
            else:
                return {"success": False, "error": f"LinkedIn {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

class TwitterProvider:
    def __init__(self, api_key: str, api_secret: str, access_token: str, access_token_secret: str):
        from requests_oauthlib import OAuth1Session
        self.session = OAuth1Session(api_key, api_secret, access_token, access_token_secret)

    def post(self, text: str, media_urls: List[str] = None, link: str = None) -> dict:
        # Twitter API v2 with OAuth 1.0a
        tweet_text = text
        if link:
            tweet_text += f" {link}"
        try:
            resp = self.session.post(
                "https://api.twitter.com/2/tweets",
                json={"text": tweet_text},
                timeout=10
            )
            if resp.status_code == 201:
                data = resp.json()
                return {"success": True, "provider": "twitter", "post_id": data["data"]["id"]}
            else:
                return {"success": False, "error": f"Twitter {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

class FacebookProvider:
    def __init__(self, page_id: str, access_token: str):
        self.page_id = page_id
        self.access_token = access_token

    def post(self, text: str, media_urls: List[str] = None, link: str = None) -> dict:
        url = f"https://graph.facebook.com/v19.0/{self.page_id}/feed"
        data = {
            "message": text,
            "access_token": self.access_token
        }
        if link:
            data["link"] = link
        try:
            resp = requests.post(url, data=data, timeout=10)
            if resp.status_code == 200:
                return {"success": True, "provider": "facebook", "post_id": resp.json().get("id")}
            else:
                return {"success": False, "error": f"Facebook {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

class TikTokProvider:
    def __init__(self, access_token: str, open_id: str):
        self.access_token = access_token
        self.open_id = open_id

    def post(self, text: str, media_urls: List[str] = None, link: str = None) -> dict:
        # TikTok for Business content publishing (Direct Post)
        # Requires a video URL (media_urls[0]) – TikTok doesn't support text-only posts.
        # If no media_urls, return error.
        if not media_urls:
            return {"success": False, "error": "TikTok requires a video URL (media_urls[0])"}
        video_url = media_urls[0]
        url = "https://open-api.tiktok.com/video/publish/"
        data = {
            "access_token": self.access_token,
            "open_id": self.open_id,
            "video_url": video_url,
            "text": text
        }
        try:
            resp = requests.post(url, data=data, timeout=15)
            if resp.status_code == 200:
                res_json = resp.json()
                if res_json.get("data", {}).get("error_code") == 0:
                    return {"success": True, "provider": "tiktok", "post_id": res_json["data"].get("item_id")}
                else:
                    return {"success": False, "error": f"TikTok API error: {res_json}"}
            else:
                return {"success": False, "error": f"TikTok {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ── Global provider instances ────────────────────────────────────
providers: Dict[str, object] = {}

def init_providers(config: dict):
    global providers
    if "linkedin" in config:
        li = config["linkedin"]
        if li.get("access_token") and li.get("urn"):
            providers["linkedin"] = LinkedInProvider(li["access_token"], li["urn"])
    if "twitter" in config:
        tw = config["twitter"]
        if all([tw.get("api_key"), tw.get("api_secret"), tw.get("access_token"), tw.get("access_token_secret")]):
            providers["twitter"] = TwitterProvider(tw["api_key"], tw["api_secret"], tw["access_token"], tw["access_token_secret"])
    if "facebook" in config:
        fb = config["facebook"]
        if fb.get("page_id") and fb.get("access_token"):
            providers["facebook"] = FacebookProvider(fb["page_id"], fb["access_token"])
    if "tiktok" in config:
        tt = config["tiktok"]
        if tt.get("access_token") and tt.get("open_id"):
            providers["tiktok"] = TikTokProvider(tt["access_token"], tt["open_id"])

def post_to_platform(platform: str, text: str, media_urls: List[str] = None, link: str = None) -> dict:
    prov = providers.get(platform)
    if not prov:
        return {"success": False, "error": f"Platform {platform} not configured"}
    return prov.post(text, media_urls, link)

# ── Scheduled post checking ──────────────────────────────────────
def load_scheduled_posts(schedule_file: str) -> List[dict]:
    try:
        with open(schedule_file, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

def save_scheduled_posts(schedule_file: str, posts: List[dict]):
    with open(schedule_file, "w") as f:
        json.dump(posts, f, indent=2, default=str)

def process_scheduled(schedule_file: str, state: dict):
    """Check and publish due posts, update state."""
    posts = load_scheduled_posts(schedule_file)
    now = datetime.now(timezone.utc).isoformat()
    updated = []
    published = []
    for post in posts:
        scheduled_at = post.get("scheduled_at")
        if not scheduled_at:
            updated.append(post)
            continue
        if scheduled_at <= now and post.get("id") not in state.get("completed", set()):
            # due
            platform = post.get("platform", "")
            text = post.get("text", "")
            media_urls = post.get("media_urls")
            link = post.get("link")
            result = post_to_platform(platform, text, media_urls, link)
            if result.get("success"):
                _post(f"Published on {platform}: {text[:80]}", "info", {"post": post, "result": result})
                state.setdefault("completed", set()).add(post.get("id", scheduled_at))
                published.append(post)
            else:
                _post(f"Failed to publish on {platform}: {result.get('error')}", "error")
                updated.append(post)  # keep for retry
        else:
            updated.append(post)
    if published:
        save_scheduled_posts(schedule_file, updated)

# ── HTTP API for on‑demand posting ───────────────────────────────
class SocialMediaHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/post":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                platform = data.get("platform")
                text = data.get("text")
                media_urls = data.get("media_urls")
                link = data.get("link")
                if not platform or not text:
                    self._respond(400, {"error": "Missing 'platform' or 'text'"})
                    return
                result = post_to_platform(platform, text, media_urls, link)
                if result.get("success"):
                    _post(f"On‑demand post to {platform}: {text[:80]}", "info", result)
                    self._respond(200, {"status": "posted", "details": result})
                else:
                    self._respond(500, {"status": "failed", "error": result.get("error")})
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_http(port: int):
    server = HTTPServer(("0.0.0.0", port), SocialMediaHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Social Publisher API on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    global providers
    _post("Social Media Publisher Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        init_providers(config)
        schedule_file = config.get("schedule_file", "scheduled_posts.json")
        poll_interval = int(config.get("poll_interval_seconds", 60))
        port = int(config.get("http_port", 9610))
        start_http(port)

        state = {"completed": set()}  # simple in‑memory state, could be persisted

        while True:
            process_scheduled(schedule_file, state)
            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
