#!/usr/bin/env python3
"""
wechat_official_account_bot.py — WeChat Official Account Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Publishes scheduled articles to a WeChat Official Account
and auto‑responds to follower text messages via the
WeChat public platform API.  Attachable to the Nazgul
BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `wechat_official_config.json` in the same directory:

{
  "wechat": {
    "appid": "wx1234567890abcdef",
    "appsecret": "your_secret",
    "token": "your_wechat_verify_token",
    "encoding_aes_key": null            // optional, for encrypted mode; leave null for plain
  },
  "publishing": {
    "enabled": true,
    "articles_file": "wechat_scheduled_articles.json"
  },
  "auto_reply": {
    "enabled": true,
    "webhook_port": 9750,
    "default_reply": "Thanks for your message! We'll get back to you soon.",
    "llm": null                         // optional OpenAI config for contextual replies
  },
  "state_file": "wechat_official_state.json",
  "heartbeat_interval": 30
}

Scheduled articles file (`wechat_scheduled_articles.json`) – array:
[
  {
    "title": "Latest Product Updates",
    "content": "<p>HTML content</p>",
    "thumb_media_id": "permanent_media_id_of_cover_image",
    "content_source_url": "https://example.com/article",
    "digest": "Short summary",
    "scheduled_at": "2025-03-10T10:00:00Z"
  }
]

Notes:
- To publish, the account must be a verified service account or
  have the free publish permission.  The bot uses the free publish API.
- Auto‑reply uses the message callback: you must set the server URL
  in WeChat's developer settings to point to this bot's webhook
  (e.g. http://your-server:9750/wechat).
"""

import json
import os
import time
import threading
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "wechat_official_account_bot"
BOT_NAME = "WeChat Official Account"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "wechat_official_config.json"
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
        return {"access_token": None, "access_token_expires": 0, "published_hashes": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── WeChat API helpers ───────────────────────────────────────────
WECHAT_API = "https://api.weixin.qq.com"

def get_access_token(appid: str, secret: str) -> Optional[str]:
    """Obtain a new access token (valid for 7200s)."""
    url = f"{WECHAT_API}/cgi-bin/token"
    params = {
        "grant_type": "client_credential",
        "appid": appid,
        "secret": secret
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if "access_token" in data:
                return data["access_token"]
            else:
                _post(f"WeChat token error: {data}", "error")
                return None
        else:
            _post(f"WeChat token HTTP {resp.status_code}", "error")
            return None
    except Exception as e:
        _post(f"WeChat token request failed: {e}", "error")
        return None

def ensure_access_token(config: dict, state: dict) -> Optional[str]:
    """Return a valid access token, refreshing if needed."""
    now = time.time()
    token = state.get("access_token")
    expires = state.get("access_token_expires", 0)
    if token and now < expires - 300:  # 5 min buffer
        return token
    # refresh
    appid = config["wechat"]["appid"]
    secret = config["wechat"]["appsecret"]
    new_token = get_access_token(appid, secret)
    if new_token:
        state["access_token"] = new_token
        state["access_token_expires"] = now + 7200
        return new_token
    return None

def free_publish_article(access_token: str, article: dict) -> bool:
    """
    Publish a draft article via the free publish API.
    The article dict must contain: title, content, thumb_media_id,
    content_source_url (optional), digest (optional).
    """
    url = f"{WECHAT_API}/cgi-bin/freepublish/submit?access_token={access_token}"
    payload = {
        "articles": [
            {
                "title": article["title"],
                "content": article["content"],  # HTML
                "thumb_media_id": article["thumb_media_id"],
                "content_source_url": article.get("content_source_url", ""),
                "digest": article.get("digest", ""),
                "need_open_comment": 0,
                "only_fans_can_comment": 0
            }
        ]
    }
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("errcode") == 0:
                return True
            else:
                _post(f"Free publish error: {data}", "error")
                return False
        else:
            _post(f"Free publish HTTP {resp.status_code}: {resp.text[:200]}", "error")
            return False
    except Exception as e:
        _post(f"Free publish request failed: {e}", "error")
        return False

def process_scheduled_articles(config: dict, state: dict):
    """Publish scheduled articles that are due."""
    if not config.get("publishing", {}).get("enabled"):
        return
    file_path = config["publishing"].get("articles_file", "wechat_scheduled_articles.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            articles = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled articles: {e}", "error")
        return

    if not articles:
        return

    token = ensure_access_token(config, state)
    if not token:
        _post("Cannot obtain WeChat access token for publishing", "error")
        return

    now = datetime.now(timezone.utc)
    remaining = []
    published_hashes = set(state.get("published_hashes", []))

    for article in articles:
        scheduled_at_str = article.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(article)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(article)
            continue

        item_hash = str(hash(json.dumps(article, sort_keys=True)))
        if item_hash in published_hashes:
            continue  # already published

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            if free_publish_article(token, article):
                _post(f"Published article: {article['title'][:50]}", "info")
                published_hashes.add(item_hash)
                # success, remove from queue
                continue
            else:
                _post("Failed to publish article, keeping for retry", "error")
        remaining.append(article)

    state["published_hashes"] = list(published_hashes)[-500:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Webhook server for message callback ──────────────────────────
class WeChatWebhookHandler(BaseHTTPRequestHandler):
    config: dict = None
    state: dict = None

    def do_GET(self):
        """Verify the WeChat server URL."""
        if not self.path.startswith("/wechat"):
            self.send_error(404)
            return
        # Parse query params
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        signature = params.get("signature", [None])[0]
        timestamp = params.get("timestamp", [None])[0]
        nonce = params.get("nonce", [None])[0]
        echostr = params.get("echostr", [None])[0]

        if not all([signature, timestamp, nonce, echostr]):
            self.send_error(400)
            return

        # Validate signature
        token = self.config["wechat"].get("token", "")
        if not self._check_signature(signature, timestamp, nonce, token):
            self.send_error(403)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(echostr.encode())

    def do_POST(self):
        """Receive incoming message and auto‑reply."""
        if not self.path.startswith("/wechat"):
            self.send_error(404)
            return
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)
        try:
            xml_data = ET.fromstring(body)
        except ET.ParseError:
            self.send_error(400)
            return

        # Extract message type and fields
        msg_type = xml_data.findtext("MsgType")
        from_user = xml_data.findtext("FromUserName")
        to_user = xml_data.findtext("ToUserName")
        if msg_type != "text" or not from_user or not to_user:
            # Only handle text messages; for others, return empty 200
            self.send_response(200)
            self.end_headers()
            return

        content = xml_data.findtext("Content", "")
        # Build auto‑reply
        reply = self._generate_reply(content)
        # Construct response XML
        response_xml = self._build_text_response(to_user, from_user, reply)
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.end_headers()
        self.wfile.write(response_xml.encode("utf-8"))

    def _check_signature(self, signature, timestamp, nonce, token):
        """Verify WeChat signature."""
        tmp_list = sorted([token, timestamp, nonce])
        tmp_str = "".join(tmp_list)
        sha1 = hashlib.sha1(tmp_str.encode()).hexdigest()
        return sha1 == signature

    def _build_text_response(self, from_user, to_user, content):
        """Create XML for a text reply."""
        xml = f"""<xml>
  <ToUserName><![CDATA[{to_user}]]></ToUserName>
  <FromUserName><![CDATA[{from_user}]]></FromUserName>
  <CreateTime>{int(time.time())}</CreateTime>
  <MsgType><![CDATA[text]]></MsgType>
  <Content><![CDATA[{content}]]></Content>
</xml>"""
        return xml

    def _generate_reply(self, incoming_text: str) -> str:
        """Generate auto‑reply text, optionally using LLM."""
        default = self.config.get("auto_reply", {}).get("default_reply", "Thanks for your message!")
        llm_cfg = self.config.get("auto_reply", {}).get("llm")
        if not llm_cfg or not llm_cfg.get("api_key"):
            return default
        # Use OpenAI to generate a contextual reply
        try:
            import openai
            client = openai.OpenAI(api_key=llm_cfg["api_key"])
            prompt = f"The user says: '{incoming_text}'. Write a short, friendly reply in Chinese that addresses their query and mentions our product website https://example.com."
            response = client.chat.completions.create(
                model=llm_cfg.get("model", "gpt-4o-mini"),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                max_tokens=300
            )
            reply = response.choices[0].message.content.strip()
            return reply
        except Exception as e:
            _post(f"LLM auto‑reply failed: {e}", "error")
            return default

    def log_message(self, *args):
        pass

def start_webhook(config: dict, state: dict):
    port = config.get("auto_reply", {}).get("webhook_port", 9750)
    if not config.get("auto_reply", {}).get("enabled", False):
        return
    WeChatWebhookHandler.config = config
    WeChatWebhookHandler.state = state
    server = HTTPServer(("0.0.0.0", port), WeChatWebhookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"WeChat webhook listening on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("WeChat Official Account Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "wechat_official_state.json")
        state = load_state(state_file)

        # Start webhook server (if not already running)
        start_webhook(config, state)

        # Process scheduled articles every 30 seconds
        while True:
            process_scheduled_articles(config, state)
            save_state(state_file, state)
            _heartbeat()
            time.sleep(30)

if __name__ == "__main__":
    main()
