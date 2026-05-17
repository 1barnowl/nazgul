#!/usr/bin/env python3
"""
whatsapp_broadcast_bot.py — WhatsApp Business Broadcast Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sends scheduled product promotions via the WhatsApp Cloud API
and auto‑replies to customer inquiries using a webhook.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `whatsapp_broadcast_config.json` in the same directory:

{
  "whatsapp": {
    "phone_number_id": "123456789012345",
    "access_token": "EAAG...",
    "verify_token": "my_webhook_verify_token",
    "api_version": "v19.0"
  },
  "broadcast": {
    "enabled": true,
    "scheduled_file": "whatsapp_scheduled_broadcasts.json"
  },
  "auto_reply": {
    "enabled": true,
    "webhook_port": 9710,
    "template_message": "Thank you for your message! We'll get back to you shortly.",
    "llm": null                           // optional OpenAI config for contextual replies
  },
  "state_file": "whatsapp_broadcast_state.json",
  "heartbeat_interval": 30
}

Scheduled broadcasts file (`whatsapp_scheduled_broadcasts.json`) – an array:
[
  {
    "to": "1234567890",                      // recipient phone number
    "type": "text",                          // "text" or "template"
    "text": "Hi! Don't miss our weekend sale: https://example.com",
    "scheduled_at": "2025-02-20T12:00:00Z"
  },
  {
    "to": "9876543210",
    "type": "template",
    "template_name": "promo_alert",
    "language_code": "en_US",
    "scheduled_at": "2025-02-20T14:00:00Z"
  }
]
"""

import json
import os
import time
import threading
import asyncio
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "whatsapp_broadcast_bot"
BOT_NAME = "WhatsApp Business Broadcast"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "whatsapp_broadcast_config.json"
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
        return {"sent_broadcasts": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── WhatsApp Cloud API helpers ───────────────────────────────────
WHATSAPP_API = "https://graph.facebook.com"

def send_whatsapp_message(phone_number_id: str, access_token: str, to: str,
                          msg_type: str, **kwargs) -> Optional[str]:
    """
    Send a WhatsApp message. Returns the message ID on success.
    """
    url = f"{WHATSAPP_API}/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    data = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": msg_type
    }
    if msg_type == "text":
        data["text"] = {"preview_url": False, "body": kwargs["text"]}
    elif msg_type == "template":
        data["template"] = {
            "name": kwargs["template_name"],
            "language": {"code": kwargs.get("language_code", "en_US")}
        }
        # Optional components (header, body parameters) can be added
        if "components" in kwargs:
            data["template"]["components"] = kwargs["components"]
    else:
        raise ValueError("Unsupported message type")

    try:
        resp = requests.post(url, headers=headers, json=data, timeout=15)
        if resp.status_code in (200, 201):
            result = resp.json()
            return result.get("messages", [{}])[0].get("id")
        else:
            _post(f"WhatsApp API error {resp.status_code}: {resp.text[:300]}", "error")
            return None
    except Exception as e:
        _post(f"WhatsApp API request error: {e}", "error")
        return None

# ── Scheduled broadcast processing ───────────────────────────────
def process_scheduled_broadcasts(config: dict, state: dict):
    """Send due scheduled broadcasts."""
    if not config.get("broadcast", {}).get("enabled", False):
        return
    file_path = config["broadcast"].get("scheduled_file", "whatsapp_scheduled_broadcasts.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled broadcasts: {e}", "error")
        return

    if not scheduled:
        return

    phone_number_id = config["whatsapp"]["phone_number_id"]
    access_token = config["whatsapp"]["access_token"]
    now = datetime.now(timezone.utc)
    remaining = []
    sent_ids = set(state.get("sent_broadcasts", []))

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

        # Unique ID based on content hash
        content = json.dumps(item, sort_keys=True)
        item_hash = str(hash(content))
        if item_hash in sent_ids:
            continue  # already sent

        if now - timedelta(seconds=30) <= scheduled_dt <= now + timedelta(seconds=30):
            to = item["to"]
            msg_type = item.get("type", "text")
            if msg_type == "text":
                text = item.get("text", "")
                if not text:
                    _post("Missing text in scheduled broadcast", "warning")
                    continue
                message_id = send_whatsapp_message(phone_number_id, access_token, to, "text", text=text)
            elif msg_type == "template":
                template_name = item.get("template_name")
                if not template_name:
                    _post("Missing template_name", "warning")
                    continue
                language_code = item.get("language_code", "en_US")
                components = item.get("components")
                message_id = send_whatsapp_message(phone_number_id, access_token, to, "template",
                                                   template_name=template_name,
                                                   language_code=language_code,
                                                   components=components)
            else:
                _post(f"Unsupported message type: {msg_type}", "warning")
                continue

            if message_id:
                _post(f"Broadcast sent to {to}: {message_id}", "info", {"to": to, "message_id": message_id})
                sent_ids.add(item_hash)
                # success: remove from queue
                continue
            else:
                _post(f"Failed to send broadcast to {to}", "error")
        remaining.append(item)

    state["sent_broadcasts"] = list(sent_ids)[-1000:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Webhook server for incoming messages (auto‑reply) ────────────
class WhatsAppWebhookHandler(BaseHTTPRequestHandler):
    config: dict = None
    state: dict = None

    def do_GET(self):
        """Verify webhook."""
        if self.path.startswith("/whatsapp"):
            params = {}
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                params = dict(qc.split("=") for qc in query.split("&"))
            mode = params.get("hub.mode")
            token = params.get("hub.verify_token")
            challenge = params.get("hub.challenge")
            expected = self.config["whatsapp"].get("verify_token", "")
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
        """Receive incoming messages."""
        if self.path.startswith("/whatsapp"):
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
            except Exception:
                self.send_error(400)
                return

            # Process each entry
            for entry in data.get("entry", []):
                for change in entry.get("changes", []):
                    value = change.get("value", {})
                    messages = value.get("messages", [])
                    for msg in messages:
                        if msg.get("type") == "text":
                            from_number = msg.get("from")
                            text_body = msg.get("text", {}).get("body", "")
                            self._handle_incoming(from_number, text_body)
            self.send_response(200)
            self.end_headers()
        else:
            self.send_error(404)

    def _handle_incoming(self, from_number: str, message: str):
        """Auto‑reply to a customer message."""
        auto_reply_cfg = self.config.get("auto_reply", {})
        if not auto_reply_cfg.get("enabled"):
            return
        reply_text = auto_reply_cfg.get("template_message", "Thanks for your message!")
        # Optionally use LLM
        llm_cfg = auto_reply_cfg.get("llm")
        if llm_cfg and llm_cfg.get("api_key"):
            # Generate contextual reply
            from openai import OpenAI
            try:
                client = OpenAI(api_key=llm_cfg["api_key"])
                prompt = f"The customer says: '{message}'. Write a friendly, helpful reply that thanks them and offers assistance. Include our website: https://example.com"
                response = client.chat.completions.create(
                    model=llm_cfg.get("model", "gpt-4o-mini"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=300
                )
                reply_text = response.choices[0].message.content.strip()
            except Exception as e:
                _post(f"LLM auto‑reply failed: {e}", "error")
        # Send reply
        phone_number_id = self.config["whatsapp"]["phone_number_id"]
        access_token = self.config["whatsapp"]["access_token"]
        msg_id = send_whatsapp_message(phone_number_id, access_token, from_number, "text", text=reply_text)
        if msg_id:
            _post(f"Auto‑replied to {from_number}: {reply_text[:60]}...", "info", {"to": from_number, "message_id": msg_id})
        else:
            _post(f"Failed to auto‑reply to {from_number}", "error")

    def log_message(self, *args):
        pass

def start_webhook(config: dict, state: dict):
    port = config["auto_reply"].get("webhook_port", 9710)
    WhatsAppWebhookHandler.config = config
    WhatsAppWebhookHandler.state = state
    server = HTTPServer(("0.0.0.0", port), WhatsAppWebhookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"WhatsApp webhook listening on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("WhatsApp Business Broadcast Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "whatsapp_broadcast_state.json")
        state = load_state(state_file)

        # Start webhook if auto‑reply enabled
        if config.get("auto_reply", {}).get("enabled"):
            start_webhook(config, state)

        # Process scheduled broadcasts every 30 seconds
        while True:
            process_scheduled_broadcasts(config, state)
            save_state(state_file, state)
            _heartbeat()
            time.sleep(30)

if __name__ == "__main__":
    main()
