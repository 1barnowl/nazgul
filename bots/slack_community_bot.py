#!/usr/bin/env python3
"""
slack_community_bot.py — Slack Community Group Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auto‑posts scheduled messages and responds to keywords
in public Slack channels. Uses Slack’s Socket Mode so
no public webhook URL is required.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install slack-sdk requests

Configuration
─────────────
Place `slack_community_config.json` in the same directory:

{
  "slack": {
    "bot_token": "xoxb-...",
    "app_token": "xapp-..."               // Socket Mode token (required)
  },
  "monitoring": {
    "enabled": true,
    "channel_ids": [],                    // empty = all channels the bot is in
    "keywords": ["help", "question", "tool", "automation"],
    "reply_template": "I noticed you mentioned \"{keyword}\". Our resource might help: {link}",
    "link": "https://your-product.com",
    "cooldown_seconds": 300,
    "max_replies_per_channel_per_hour": 2
  },
  "scheduled_messages_file": "slack_scheduled_posts.json",
  "state_file": "slack_community_state.json",
  "heartbeat_interval": 30
}

Scheduled posts (`slack_scheduled_posts.json`) – array of objects:
[
  {
    "channel": "C1234567890",
    "text": "Don't miss our upcoming webinar!",
    "scheduled_at": "2025-05-20T12:00:00Z"
  }
]
"""

import json
import os
import time
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "slack_community_bot"
BOT_NAME = "Slack Community Group"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "slack_community_config.json"
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
        return {
            "sent_scheduled_ids": [],
            "channel_cooldowns": {},
            "channel_reply_counts": {},
            "hour_start": time.time()
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Slack client ─────────────────────────────────────────────────
class SlackBot:
    def __init__(self, config: dict, state: dict):
        self.config = config
        self.state = state
        self.bot_token = config["slack"]["bot_token"]
        self.app_token = config["slack"]["app_token"]
        self.web_client = WebClient(token=self.bot_token)
        self.socket_client = SocketModeClient(
            app_token=self.app_token,
            web_client=self.web_client,
            auto_reconnect_enabled=True
        )
        self.monitoring = config.get("monitoring", {})
        self.keywords = [kw.lower() for kw in self.monitoring.get("keywords", [])]
        self.reply_template = self.monitoring.get("reply_template", "")
        self.link = self.monitoring.get("link", "")
        self.cooldown_seconds = float(self.monitoring.get("cooldown_seconds", 300))
        self.max_replies = int(self.monitoring.get("max_replies_per_channel_per_hour", 2))
        self.channel_ids = set(self.monitoring.get("channel_ids", []))

        # Register socket mode handlers
        self.socket_client.socket_mode_request_listeners.append(self._handle_events)

    def _handle_events(self, client: SocketModeClient, req: SocketModeRequest):
        # Acknowledge the request immediately
        response = SocketModeResponse(envelope_id=req.envelope_id)
        client.send_socket_mode_response(response)

        if req.type == "events_api":
            event = req.payload.get("event", {})
            event_type = event.get("type", "")
            if event_type == "message" and "subtype" not in event:
                channel = event.get("channel", "")
                user = event.get("user", "")
                text = event.get("text", "")
                ts = event.get("ts", "")
                # Ignore messages from the bot itself
                try:
                    bot_info = self.web_client.auth_test()
                    bot_user_id = bot_info["user_id"]
                except Exception:
                    bot_user_id = None
                if bot_user_id and user == bot_user_id:
                    return
                # Keyword detection
                if self._should_reply(channel, text):
                    keyword = self._match_keyword(text)
                    if keyword:
                        reply = self.reply_template.replace("{keyword}", keyword).replace("{link}", self.link)
                        try:
                            self.web_client.chat_postMessage(channel=channel, text=reply, thread_ts=ts)
                            _post(f"Replied in channel {channel} to keyword '{keyword}'", "info")
                            # Update state
                            now = time.time()
                            self.state.setdefault("channel_cooldowns", {})[channel] = now
                            # Reset hourly counts if needed
                            if now - self.state.get("hour_start", 0) > 3600:
                                self.state["channel_reply_counts"] = {}
                                self.state["hour_start"] = now
                            counts = self.state.setdefault("channel_reply_counts", {})
                            counts[channel] = counts.get(channel, 0) + 1
                        except Exception as e:
                            _post(f"Failed to reply in channel {channel}: {e}", "error")

    def _should_reply(self, channel: str, text: str) -> bool:
        if not self.monitoring.get("enabled"):
            return False
        # Channel filter
        if self.channel_ids and channel not in self.channel_ids:
            return False
        # Keyword presence
        text_lower = text.lower()
        if not any(kw in text_lower for kw in self.keywords):
            return False
        # Cooldown
        last = self.state.get("channel_cooldowns", {}).get(channel, 0)
        if time.time() - last < self.cooldown_seconds:
            return False
        # Per‑channel hourly cap
        now = time.time()
        if now - self.state.get("hour_start", 0) > 3600:
            self.state["channel_reply_counts"] = {}
            self.state["hour_start"] = now
        counts = self.state.get("channel_reply_counts", {})
        if counts.get(channel, 0) >= self.max_replies:
            return False
        return True

    def _match_keyword(self, text: str) -> Optional[str]:
        text_lower = text.lower()
        for kw in self.keywords:
            if kw in text_lower:
                return kw
        return None

    def start(self):
        self.socket_client.connect()
        _post("Slack bot started via Socket Mode", "info")

# ── Scheduled message processing (background thread) ─────────────
def process_scheduled_messages(config: dict, state: dict):
    file_path = config.get("scheduled_messages_file", "slack_scheduled_posts.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            messages = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled posts: {e}", "error")
        return

    if not messages:
        return

    bot_token = config["slack"]["bot_token"]
    client = WebClient(token=bot_token)
    now = datetime.now(timezone.utc)
    remaining = []
    sent_ids = set(state.get("sent_scheduled_ids", []))

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
        if item_hash in sent_ids:
            continue  # already sent

        if now - timedelta(seconds=30) <= scheduled_dt <= now + timedelta(seconds=30):
            channel = msg.get("channel")
            text = msg.get("text", "")
            if not channel or not text:
                _post("Invalid scheduled message entry", "warning")
                continue
            try:
                client.chat_postMessage(channel=channel, text=text)
                _post(f"Scheduled message sent to channel {channel}", "info")
                sent_ids.add(item_hash)
                # success: do not add to remaining
                continue
            except Exception as e:
                _post(f"Failed to send scheduled message: {e}", "error")
                # keep for retry
        remaining.append(msg)

    state["sent_scheduled_ids"] = list(sent_ids)[-500:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Slack Community Group Bot online")
    # Load config
    try:
        with open(CONFIG_PATH, "r") as f:
            config = json.load(f)
    except Exception as e:
        _post(f"Config error: {e}", "error")
        return

    state_file = config.get("state_file", "slack_community_state.json")
    state = load_state(state_file)

    # Start Slack Socket Mode client
    slack_bot = SlackBot(config, state)
    slack_bot.start()

    # Background thread for scheduled messages and state saving
    def background_loop():
        while True:
            try:
                process_scheduled_messages(config, state)
                save_state(state_file, state)
                _heartbeat()
            except Exception as e:
                _post(f"Background error: {e}", "error")
            time.sleep(30)

    threading.Thread(target=background_loop, daemon=True).start()

    # Keep main thread alive
    while True:
        time.sleep(10)

if __name__ == "__main__":
    main()
