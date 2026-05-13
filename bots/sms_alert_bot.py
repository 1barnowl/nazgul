#!/usr/bin/env python3
"""
sms_alert_bot.py — SMS Alert Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sends urgent notifications via Twilio.
Exposes an HTTP API and optional file
watch for other bots to trigger SMS alerts.

Attachable to the Nazgul BotController
(http://localhost:8765).

Requirements
────────────
    pip install twilio requests

Configuration
─────────────
Place `sms_alert_config.json` in the same directory:

{
  "twilio": {
    "account_sid": "ACxxxxxxxxxx",
    "auth_token": "your_auth_token",
    "from_number": "+1234567890"
  },
  "http_port": 9600,
  "file_watch": {
    "enabled": false,
    "directory": "/data/outgoing_sms",
    "processed_directory": "/data/sent_sms"
  },
  "heartbeat_interval": 30
}
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "sms_alert_bot"
BOT_NAME = "SMS Alert"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "sms_alert_config.json"
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

# ── Twilio client ────────────────────────────────────────────────
class TwilioSMS:
    def __init__(self, account_sid: str, auth_token: str, from_number: str):
        self.account_sid = account_sid
        self.auth_token  = auth_token
        self.from_number = from_number

    def send(self, to: str, message: str) -> dict:
        """Send an SMS and return result dict with success/error."""
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.account_sid}/Messages.json"
        data = {
            "From": self.from_number,
            "To":   to,
            "Body": message
        }
        try:
            resp = requests.post(url, data=data,
                                 auth=(self.account_sid, self.auth_token),
                                 timeout=10)
            if resp.status_code == 201:
                json_resp = resp.json()
                return {"success": True, "sid": json_resp.get("sid"), "provider": "twilio"}
            else:
                error_text = resp.text[:200]
                return {"success": False, "error": f"Twilio error {resp.status_code}: {error_text}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ── Global sender instance ────────────────────────────────────────
sms_sender = None

def init_sender(config: dict):
    global sms_sender
    tw_cfg = config.get("twilio", {})
    account_sid = tw_cfg.get("account_sid")
    auth_token  = tw_cfg.get("auth_token")
    from_number = tw_cfg.get("from_number")
    if not all([account_sid, auth_token, from_number]):
        raise ValueError("Twilio configuration incomplete (account_sid, auth_token, from_number required)")
    sms_sender = TwilioSMS(account_sid, auth_token, from_number)

def send_sms(to: str, message: str) -> dict:
    return sms_sender.send(to, message)

# ── HTTP API handler ─────────────────────────────────────────────
class SMSHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/send":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                to      = data.get("to")
                message = data.get("message", "")
                if not to or not message:
                    self._respond(400, {"error": "Missing 'to' or 'message' field"})
                    return
                result = send_sms(to, message)
                if result.get("success"):
                    _post(f"SMS sent to {to}: {message[:80]}", "info", {"to": to, "message": message, "result": result})
                    self._respond(200, {"status": "sent", "details": result})
                else:
                    _post(f"Failed to send SMS to {to}: {result.get('error')}", "error")
                    self._respond(500, {"status": "failed", "error": result.get("error")})
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "Not found"})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_http(port: int):
    server = HTTPServer(("0.0.0.0", port), SMSHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"SMS Alert API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, processed_dir: str, processed: set):
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(directory, fname)
        if fpath in processed:
            continue
        try:
            with open(fpath, "r") as f:
                data = json.load(f)
            to      = data.get("to")
            message = data.get("message", "")
            if not to or not message:
                _post(f"Invalid SMS file {fname}: missing to/message", "warning")
                err_path = os.path.join(processed_dir, fname + ".error")
                os.rename(fpath, err_path)
                processed.add(fpath)
                continue
            result = send_sms(to, message)
            if result.get("success"):
                _post(f"File‑based SMS sent to {to}", "info")
                dest = os.path.join(processed_dir, fname)
                os.rename(fpath, dest)
            else:
                _post(f"Failed to send file‑based SMS: {result.get('error')}", "error")
                err_path = os.path.join(processed_dir, fname + ".error")
                os.rename(fpath, err_path)
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing SMS file {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    global sms_sender
    _post("SMS Alert Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        try:
            init_sender(config)
        except Exception as e:
            _post(f"Twilio init error: {e}", "error")
            time.sleep(60)
            continue

        port = int(config.get("http_port", 9600))
        start_http(port)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            processed_dir = file_cfg.get("processed_directory")
            if directory and processed_dir:
                os.makedirs(processed_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, processed_dir, processed_cache)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
