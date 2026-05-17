#!/usr/bin/env python3
"""
line_official_account_bot.py — LINE Official Account Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Pushes messages and rich content to LINE followers in
Japan/Thailand, and auto‑replies to incoming messages
via the LINE Messaging API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install line-bot-sdk requests

Configuration
─────────────
Place `line_official_config.json` in the same directory:

{
  "line": {
    "channel_access_token": "YOUR_CHANNEL_ACCESS_TOKEN",
    "channel_secret": "YOUR_CHANNEL_SECRET"
  },
  "push": {
    "enabled": true,
    "scheduled_messages_file": "line_scheduled_messages.json"
  },
  "auto_reply": {
    "enabled": true,
    "webhook_port": 9770,
    "default_reply": "Thank you for contacting us! We will get back to you shortly.",
    "llm": null
  },
  "state_file": "line_official_state.json",
  "heartbeat_interval": 30
}

Scheduled messages file (`line_scheduled_messages.json`) – array:
[
  {
    "type": "text",
    "text": "Hello! Our weekend sale is now live.",
    "to": "ALL",
    "scheduled_at": "2025-02-10T12:00:00Z"
  },
  {
    "type": "flex",
    "alt_text": "This is a flex message",
    "contents": { ... },
    "to": ["U123456789abcdef", "U987654321fedcba"],
    "scheduled_at": "2025-02-11T15:00:00Z"
  }
]

`to` can be a string "ALL" (broadcast to all followers),
a single user ID, or a list of user IDs (multicast).
"""

import json
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Union

import requests
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    FlexSendMessage, QuickReply, QuickReplyButton,
    MessageAction
)

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "line_official_account_bot"
BOT_NAME = "LINE Official Account"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "line_official_config.json"
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
        return {"sent_hashes": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── LINE Messaging API helpers ───────────────────────────────────
def get_line_api(config: dict) -> LineBotApi:
    token = config["line"]["channel_access_token"]
    return LineBotApi(token)

def send_broadcast(line_api: LineBotApi, message) -> Optional[str]:
    """
    Send a broadcast message to all followers.
    Returns the request ID or None.
    """
    try:
        if isinstance(message, str):
            resp = line_api.broadcast(TextSendMessage(text=message))
        elif isinstance(message, dict):
            # Flex or other rich message
            if message.get("type") == "flex":
                msg_obj = FlexSendMessage(
                    alt_text=message.get("alt_text", "Flex Message"),
                    contents=message.get("contents")
                )
            else:
                _post(f"Unsupported message type for broadcast: {message.get('type')}", "error")
                return None
            resp = line_api.broadcast(msg_obj)
        else:
            _post("Invalid message format", "error")
            return None
        return resp.request_id if resp else None
    except Exception as e:
        _post(f"LINE broadcast error: {e}", "error")
        return None

def send_multicast(line_api: LineBotApi, to: List[str], message) -> Optional[str]:
    """Send a multicast message to specific user IDs."""
    try:
        if isinstance(message, str):
            resp = line_api.multicast(to, TextSendMessage(text=message))
        elif isinstance(message, dict):
            if message.get("type") == "flex":
                msg_obj = FlexSendMessage(
                    alt_text=message.get("alt_text", "Flex Message"),
                    contents=message.get("contents")
                )
            else:
                _post(f"Unsupported message type for multicast: {message.get('type')}", "error")
                return None
            resp = line_api.multicast(to, msg_obj)
        else:
            _post("Invalid message format", "error")
            return None
        return resp.request_id if resp else None
    except Exception as e:
        _post(f"LINE multicast error: {e}", "error")
        return None

def send_push(line_api: LineBotApi, user_id: str, message) -> Optional[str]:
    """Send a push message to a single user."""
    try:
        if isinstance(message, str):
            resp = line_api.push_message(user_id, TextSendMessage(text=message))
        elif isinstance(message, dict):
            if message.get("type") == "flex":
                msg_obj = FlexSendMessage(
                    alt_text=message.get("alt_text", "Flex Message"),
                    contents=message.get("contents")
                )
            else:
                _post(f"Unsupported message type for push: {message.get('type')}", "error")
                return None
            resp = line_api.push_message(user_id, msg_obj)
        else:
            _post("Invalid message format", "error")
            return None
        return resp.request_id if resp else None
    except Exception as e:
        _post(f"LINE push error: {e}", "error")
        return None

# ── Scheduled message processing ─────────────────────────────────
def process_scheduled_messages(config: dict, state: dict):
    """Send any scheduled messages that are due."""
    if not config.get("push", {}).get("enabled"):
        return
    file_path = config["push"].get("scheduled_messages_file", "line_scheduled_messages.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            messages = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled messages: {e}", "error")
        return

    if not messages:
        return

    line_api = get_line_api(config)
    now = datetime.now(timezone.utc)
    remaining = []
    sent_hashes = set(state.get("sent_hashes", []))

    for msg in messages:
        scheduled_at_str = msg.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(msg)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(msg)
            continue

        item_hash = str(hash(json.dumps(msg, sort_keys=True)))
        if item_hash in sent_hashes:
            continue  # already sent

        if now - timedelta(seconds=30) <= scheduled_dt <= now + timedelta(seconds=30):
            to = msg.get("to", "ALL")
            msg_type = msg.get("type", "text")
            message_obj = msg if msg_type != "text" else msg.get("text", "")

            success = False
            if to == "ALL":
                request_id = send_broadcast(line_api, message_obj)
                if request_id:
                    _post(f"Broadcast sent to all followers", "info")
                    success = True
            elif isinstance(to, list):
                request_id = send_multicast(line_api, to, message_obj)
                if request_id:
                    _post(f"Multicast sent to {len(to)} users", "info")
                    success = True
            elif isinstance(to, str):
                request_id = send_push(line_api, to, message_obj)
                if request_id:
                    _post(f"Push sent to user {to}", "info")
                    success = True
            else:
                _post(f"Invalid 'to' field: {to}", "error")

            if success:
                sent_hashes.add(item_hash)
                # success: remove from queue
                continue
            else:
                _post(f"Failed to send message to {to}", "error")
        remaining.append(msg)

    state["sent_hashes"] = list(sent_hashes)[-500:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Webhook server for auto‑reply ────────────────────────────────
class LineWebhookHandler(BaseHTTPRequestHandler):
    config: dict = None
    state: dict = None
    handler: WebhookHandler = None
    line_api: LineBotApi = None

    def do_POST(self):
        if self.path == "/callback":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            signature = self.headers.get("X-Line-Signature", "")
            channel_secret = self.config["line"]["channel_secret"]

            # Verify signature and handle events
            try:
                self.handler.handle(body, signature)
            except InvalidSignatureError:
                self.send_error(400, "Invalid signature")
                return
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass

def setup_webhook(config: dict, state: dict):
    if not config.get("auto_reply", {}).get("enabled"):
        return
    token = config["line"]["channel_access_token"]
    secret = config["line"]["channel_secret"]
    line_api = LineBotApi(token)
    handler = WebhookHandler(secret)

    # Define the event handler for text messages
    @handler.add(MessageEvent, message=TextMessage)
    def handle_message(event):
        reply_token = event.reply_token
        user_message = event.message.text
        # Generate reply
        reply_text = config.get("auto_reply", {}).get("default_reply", "Thanks for your message!")
        llm_cfg = config.get("auto_reply", {}).get("llm")
        if llm_cfg and llm_cfg.get("api_key"):
            # Use LLM to generate contextual reply
            import openai
            try:
                client = openai.OpenAI(api_key=llm_cfg["api_key"])
                prompt = f"User message: '{user_message}'. Write a short, friendly reply in English or Japanese. Include a link to our website https://example.com if relevant."
                response = client.chat.completions.create(
                    model=llm_cfg.get("model", "gpt-4o-mini"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=300
                )
                reply_text = response.choices[0].message.content.strip()
            except Exception as e:
                _post(f"LLM reply generation failed: {e}", "error")

        # Send reply via LINE API
        try:
            line_api.reply_message(reply_token, TextSendMessage(text=reply_text))
            _post(f"Replied to user {event.source.user_id}: {reply_text[:50]}...", "info")
        except Exception as e:
            _post(f"Failed to reply: {e}", "error")

    return handler, line_api

def start_webhook(config: dict, state: dict):
    port = config.get("auto_reply", {}).get("webhook_port", 9770)
    handler_instance, line_api_instance = setup_webhook(config, state)
    if not handler_instance:
        return

    LineWebhookHandler.config = config
    LineWebhookHandler.state = state
    LineWebhookHandler.handler = handler_instance
    LineWebhookHandler.line_api = line_api_instance

    server = HTTPServer(("0.0.0.0", port), LineWebhookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"LINE webhook server started on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("LINE Official Account Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "line_official_state.json")
        state = load_state(state_file)

        # Start webhook (non‑blocking)
        start_webhook(config, state)

        # Process scheduled messages every 30 seconds
        while True:
            process_scheduled_messages(config, state)
            save_state(state_file, state)
            _heartbeat()
            time.sleep(30)

if __name__ == "__main__":
    main()
