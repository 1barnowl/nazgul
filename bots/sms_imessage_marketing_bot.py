#!/usr/bin/env python3
"""
sms_imessage_marketing_bot.py — SMS / iMessage Marketing Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sends scheduled SMS blasts and auto‑replies to inbound
text messages with appointment booking links and offers.
Uses Twilio for SMS and a local webhook to receive replies.
iMessage marketing is not supported by Twilio’s public API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install twilio requests

Configuration
─────────────
Place `sms_marketing_config.json` in the same directory:

{
  "twilio": {
    "account_sid": "AC...",
    "auth_token": "your_auth_token",
    "from_number": "+1234567890"          // your Twilio phone number
  },
  "blasts": {
    "scheduled_file": "sms_scheduled_blasts.json"
  },
  "auto_reply": {
    "enabled": true,
    "webhook_port": 9780,
    "reply_text": "Thanks for texting us! Book a free demo: https://calendly.com/your-name",
    "llm": null
  },
  "state_file": "sms_marketing_state.json",
  "heartbeat_interval": 30
}

Scheduled blasts file (`sms_scheduled_blasts.json`) – array of objects:
[
  {
    "to": "+19876543210",
    "body": "Flash sale! 20% off this weekend. Shop now: https://your-store.com",
    "scheduled_at": "2025-06-01T10:00:00Z"
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
from typing import Dict, List, Optional, Any

import requests
from twilio.rest import Client as TwilioClient

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "sms_imessage_marketing_bot"
BOT_NAME = "SMS / iMessage Marketing"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "sms_marketing_config.json"
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
        return {"sent_blast_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Twilio client ────────────────────────────────────────────────
def get_twilio_client(config: dict) -> Optional[TwilioClient]:
    try:
        account_sid = config["twilio"]["account_sid"]
        auth_token = config["twilio"]["auth_token"]
        return TwilioClient(account_sid, auth_token)
    except KeyError:
        _post("Twilio credentials missing in config", "error")
        return None

# ── Scheduled blasts processing ──────────────────────────────────
def process_scheduled_blasts(config: dict, state: dict):
    file_path = config.get("blasts", {}).get("scheduled_file", "sms_scheduled_blasts.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            blasts = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled blasts: {e}", "error")
        return

    if not blasts:
        return

    client = get_twilio_client(config)
    if not client:
        return

    from_number = config["twilio"]["from_number"]
    now = datetime.now(timezone.utc)
    remaining = []
    sent_ids = set(state.get("sent_blast_ids", []))

    for blast in blasts:
        scheduled_at_str = blast.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(blast)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(blast)
            continue

        item_hash = str(hash(json.dumps(blast, sort_keys=True)))
        if item_hash in sent_ids:
            continue

        if now - timedelta(seconds=30) <= scheduled_dt <= now + timedelta(seconds=30):
            to = blast.get("to")
            body = blast.get("body", "")
            if not to or not body:
                _post("Invalid blast entry", "warning")
                continue
            try:
                msg = client.messages.create(
                    body=body,
                    from_=from_number,
                    to=to
                )
                _post(f"SMS sent to {to}: {msg.sid}", "info", {"to": to, "sid": msg.sid})
                sent_ids.add(item_hash)
                # success: remove from queue
                continue
            except Exception as e:
                _post(f"Failed to send SMS to {to}: {e}", "error")
        remaining.append(blast)

    state["sent_blast_ids"] = list(sent_ids)[-1000:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Webhook for auto‑reply ───────────────────────────────────────
class SMSWebhookHandler(BaseHTTPRequestHandler):
    config: dict = None

    def do_POST(self):
        if self.path == "/sms":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            from urllib.parse import parse_qs
            params = parse_qs(body.decode())
            from_number = params.get("From", [""])[0]
            message_body = params.get("Body", [""])[0]

            auto_reply = self.config.get("auto_reply", {})
            if auto_reply.get("enabled") and from_number:
                reply_text = auto_reply.get("reply_text", "Thanks for messaging us!")
                # Optionally use LLM for contextual reply (omitted for simplicity)
                client = get_twilio_client(self.config)
                if client:
                    try:
                        client.messages.create(
                            body=reply_text,
                            from_=self.config["twilio"]["from_number"],
                            to=from_number
                        )
                        _post(f"Auto‑replied to {from_number}", "info", {"to": from_number})
                    except Exception as e:
                        _post(f"Auto‑reply failed: {e}", "error")
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass

def start_webhook(config: dict):
    port = config.get("auto_reply", {}).get("webhook_port", 9780)
    if not config.get("auto_reply", {}).get("enabled"):
        return
    SMSWebhookHandler.config = config
    server = HTTPServer(("0.0.0.0", port), SMSWebhookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"SMS webhook listening on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("SMS / iMessage Marketing Bot online")
    _post("iMessage marketing requires Apple Business Chat and is not supported via Twilio's public API. This bot only handles SMS.", "info")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "sms_marketing_state.json")
        state = load_state(state_file)

        # Start webhook for inbound messages
        start_webhook(config)

        # Continuously check scheduled blasts
        while True:
            process_scheduled_blasts(config, state)
            save_state(state_file, state)
            _heartbeat()
            time.sleep(30)

if __name__ == "__main__":
    main()
