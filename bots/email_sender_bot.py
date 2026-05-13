#!/usr/bin/env python3
"""
email_sender_bot.py — Email Sender Bot (SMTP/API)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Sends transactional emails (promotions, alerts, etc.)
via SendGrid or Amazon SES. Exposes an HTTP API and
optional file watch for other bots to trigger emails.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install requests boto3

Configuration
─────────────
Place `email_sender_config.json` in the same directory:

{
  "provider": "sendgrid",                // "sendgrid" or "ses"
  "sendgrid": {
    "api_key": "SG.your_api_key",
    "default_from": "bot@example.com"
  },
  "ses": {
    "aws_region": "us-east-1",
    "aws_access_key": "YOUR_ACCESS_KEY",
    "aws_secret_key": "YOUR_SECRET_KEY",
    "default_from": "bot@example.com"
  },
  "http_port": 9595,
  "file_watch": {
    "enabled": false,
    "directory": "/data/outgoing_email",
    "processed_directory": "/data/sent_email"
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
BOT_ID   = "email_sender_bot"
BOT_NAME = "Email Sender"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "email_sender_config.json"
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

# ── Email providers ──────────────────────────────────────────────

class SendGridProvider:
    def __init__(self, api_key: str, default_from: str):
        self.api_key = api_key
        self.default_from = default_from

    def send(self, to: str, subject: str, body: str, from_addr: str = None) -> dict:
        url = "https://api.sendgrid.com/v3/mail/send"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        data = {
            "personalizations": [{"to": [{"email": to}]}],
            "from": {"email": from_addr or self.default_from},
            "subject": subject,
            "content": [{"type": "text/plain", "value": body}]
        }
        try:
            resp = requests.post(url, json=data, headers=headers, timeout=10)
            if resp.status_code in (200, 202):
                return {"success": True, "provider": "sendgrid"}
            else:
                return {"success": False, "error": f"SendGrid error {resp.status_code}: {resp.text[:200]}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

class SESProvider:
    def __init__(self, aws_region: str, aws_access_key: str, aws_secret_key: str, default_from: str):
        import boto3
        self.client = boto3.client(
            "ses",
            region_name=aws_region,
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key
        )
        self.default_from = default_from

    def send(self, to: str, subject: str, body: str, from_addr: str = None) -> dict:
        try:
            response = self.client.send_email(
                Source=from_addr or self.default_from,
                Destination={"ToAddresses": [to]},
                Message={
                    "Subject": {"Data": subject},
                    "Body": {"Text": {"Data": body}}
                }
            )
            return {"success": True, "provider": "ses", "message_id": response["MessageId"]}
        except Exception as e:
            return {"success": False, "error": str(e)}

# ── Global sender instance ────────────────────────────────────────
email_sender = None

def init_sender(config: dict):
    global email_sender
    provider = config.get("provider", "sendgrid")
    if provider == "sendgrid":
        sg_cfg = config.get("sendgrid", {})
        api_key = sg_cfg.get("api_key")
        default_from = sg_cfg.get("default_from", "no-reply@example.com")
        if not api_key:
            raise ValueError("SendGrid API key not configured")
        email_sender = SendGridProvider(api_key, default_from)
    elif provider == "ses":
        ses_cfg = config.get("ses", {})
        email_sender = SESProvider(
            aws_region=ses_cfg.get("aws_region", "us-east-1"),
            aws_access_key=ses_cfg.get("aws_access_key"),
            aws_secret_key=ses_cfg.get("aws_secret_key"),
            default_from=ses_cfg.get("default_from", "no-reply@example.com")
        )
    else:
        raise ValueError(f"Unsupported email provider: {provider}")

def send_email(to: str, subject: str, body: str, from_addr: str = None) -> dict:
    return email_sender.send(to, subject, body, from_addr)

# ── HTTP API handler ─────────────────────────────────────────────
class EmailHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/send":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                to = data.get("to")
                subject = data.get("subject", "No subject")
                text = data.get("body") or data.get("text", "")
                from_addr = data.get("from")
                if not to or not text:
                    self._respond(400, {"error": "Missing 'to' or 'body' fields"})
                    return
                result = send_email(to, subject, text, from_addr)
                if result.get("success"):
                    _post(f"Email sent to {to}: {subject}", "info", {"to": to, "subject": subject, "result": result})
                    self._respond(200, {"status": "sent", "details": result})
                else:
                    _post(f"Failed to send email to {to}: {result.get('error')}", "error")
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
    server = HTTPServer(("0.0.0.0", port), EmailHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Email Sender API listening on port {port}", "info")

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
            to = data.get("to")
            subject = data.get("subject", "No subject")
            body = data.get("body") or data.get("text", "")
            from_addr = data.get("from")
            if not to or not body:
                _post(f"Invalid email file {fname}: missing to/body", "warning")
                # Move to processed with error suffix
                err_path = os.path.join(processed_dir, fname + ".error")
                os.rename(fpath, err_path)
                processed.add(fpath)
                continue
            result = send_email(to, subject, body, from_addr)
            if result.get("success"):
                _post(f"File‑based email sent to {to}: {subject}", "info")
                # Move to processed directory
                dest = os.path.join(processed_dir, fname)
                os.rename(fpath, dest)
            else:
                _post(f"Failed to send file‑based email: {result.get('error')}", "error")
                # Keep for retry? Move to error.
                err_path = os.path.join(processed_dir, fname + ".error")
                os.rename(fpath, err_path)
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing email file {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    global email_sender
    _post("Email Sender Bot online")
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
            _post(f"Provider init error: {e}", "error")
            time.sleep(60)
            continue

        port = int(config.get("http_port", 9595))
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
