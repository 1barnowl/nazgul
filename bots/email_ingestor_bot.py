#!/usr/bin/env python3
"""
email_ingestor_bot.py — Email Ingestor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Parses incoming emails (IMAP or webhook) to extract
transaction receipts, newsletters, leads, and other
actionable content. New emails are reported to the
Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests   (IMAP uses stdlib imaplib, email)

Configuration
─────────────
Place `email_ingestor_config.json` in the same directory:

{
  "mode": "imap",                         // "imap" or "webhook"
  "imap": {
    "host": "imap.example.com",
    "port": 993,
    "username": "user@example.com",
    "password": "your_password",
    "mailbox": "INBOX",
    "use_ssl": true
  },
  "webhook": {
    "port": 9244,
    "auth_token": null
  },
  "parsing": {
    "lead_keywords": ["interested", "pricing", "demo", "trial", "buy"],
    "receipt_keywords": ["invoice", "receipt", "order #", "payment", "transaction"],
    "newsletter_keywords": ["newsletter", "update", "weekly", "digest"],
    "extract_amounts": true
  },
  "poll_interval_minutes": 5,
  "state_file": "email_ingestor_state.json",
  "max_emails_per_run": 50
}
"""

import json
import os
import re
import time
import email
import imaplib
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, List, Dict

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "email_ingestor_bot"
BOT_NAME = "Email Ingestor"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "email_ingestor_config.json"
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

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"imap_last_uid": {}}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Email parsing utilities ─────────────────────────────────────
def parse_email_bytes(raw_bytes: bytes) -> Optional[dict]:
    """Parse raw email bytes into a structured dict."""
    try:
        msg = email.message_from_bytes(raw_bytes)
        subject = msg.get("Subject", "").strip()
        from_addr = msg.get("From", "").strip()
        date_str = msg.get("Date", "")
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                cdisp = str(part.get("Content-Disposition"))
                if ctype == "text/plain" and "attachment" not in cdisp:
                    payload = part.get_payload(decode=True)
                    if payload:
                        body += payload.decode(errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(errors="replace")
        body = body.strip()
        return {
            "subject": subject,
            "from": from_addr,
            "date": date_str,
            "body": body,
            "message_id": msg.get("Message-ID", "")
        }
    except Exception:
        return None

def extract_amounts(text: str) -> List[float]:
    """Find currency amounts in text, like $1,234.56."""
    pattern = r'\$?\b\d{1,3}(?:,\d{3})*\.\d{2}\b'
    amounts = []
    for m in re.finditer(pattern, text):
        amount_str = m.group()
        clean = amount_str.replace("$", "").replace(",", "")
        try:
            amounts.append(float(clean))
        except ValueError:
            pass
    return amounts

def categorize_email(parsed: dict, config: dict) -> List[str]:
    """Return list of category tags: lead, receipt, newsletter."""
    tags = []
    full_text = (parsed["subject"] + " " + parsed["body"]).lower()
    parsing_cfg = config.get("parsing", {})
    for kw in parsing_cfg.get("lead_keywords", []):
        if kw in full_text:
            tags.append("lead")
            break
    for kw in parsing_cfg.get("receipt_keywords", []):
        if kw in full_text:
            tags.append("receipt")
            break
    for kw in parsing_cfg.get("newsletter_keywords", []):
        if kw in full_text:
            tags.append("newsletter")
            break
    return tags

# ── IMAP fetcher ────────────────────────────────────────────────
def imap_fetch(config: dict, state: dict) -> List[dict]:
    """Connect to IMAP, fetch new emails since last UID, return parsed emails."""
    imap_cfg = config.get("imap", {})
    host = imap_cfg.get("host")
    port = imap_cfg.get("port", 993)
    username = imap_cfg.get("username")
    password = imap_cfg.get("password")
    mailbox = imap_cfg.get("mailbox", "INBOX")
    use_ssl = imap_cfg.get("use_ssl", True)

    if not all([host, username, password]):
        _post("Missing IMAP credentials", "warning")
        return []

    try:
        if use_ssl:
            conn = imaplib.IMAP4_SSL(host, port)
        else:
            conn = imaplib.IMAP4(host, port)
        conn.login(username, password)
        conn.select(mailbox, readonly=True)

        # Get last UID for this mailbox
        mailbox_key = f"{host}:{username}:{mailbox}"
        last_uid = state.get("imap_last_uid", {}).get(mailbox_key)
        if last_uid:
            search_criteria = f'UID {int(last_uid)+1}:*'
        else:
            search_criteria = 'ALL'

        typ, data = conn.uid('search', None, search_criteria)
        if typ != 'OK':
            conn.logout()
            return []

        uids = data[0].split()
        max_emails = int(config.get("max_emails_per_run", 50))
        if len(uids) > max_emails:
            uids = uids[-max_emails:]

        fetched = []
        for uid in uids:
            typ, msg_data = conn.uid('fetch', uid, '(RFC822)')
            if typ == 'OK':
                raw_email = msg_data[0][1]
                parsed = parse_email_bytes(raw_email)
                if parsed:
                    parsed["uid"] = int(uid)
                    parsed["mailbox"] = mailbox_key
                    fetched.append(parsed)

        # Update state
        if uids:
            state.setdefault("imap_last_uid", {})[mailbox_key] = int(uids[-1])

        conn.logout()
        return fetched
    except Exception as e:
        _post(f"IMAP error: {e}", "error")
        return []

# ── Webhook server ───────────────────────────────────────────────
class WebhookHandler(BaseHTTPRequestHandler):
    config: dict = None
    queue: list = None  # thread-safe list for incoming emails

    def do_POST(self):
        if self.path == "/webhook":
            auth = self.config.get("webhook", {}).get("auth_token")
            if auth and self.headers.get("Authorization") != f"Bearer {auth}":
                self._respond(401, {"error": "unauthorized"})
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length)
                data = json.loads(body)
                # Expect a dict with at least subject, body, from (optional)
                if not data.get("subject") or not data.get("body"):
                    self._respond(400, {"error": "subject and body required"})
                    return
                self.queue.append(data)
                self._respond(200, {"status": "queued"})
            except Exception as e:
                self._respond(400, {"error": str(e)})
        else:
            self._respond(404, {})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_webhook(config: dict, queue: list):
    webhook_cfg = config.get("webhook", {})
    if not webhook_cfg.get("port"):
        return
    port = int(webhook_cfg["port"])
    WebhookHandler.config = config
    WebhookHandler.queue = queue
    server = HTTPServer(("0.0.0.0", port), WebhookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Email webhook listening on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Email Ingestor Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        mode = config.get("mode", "imap")
        poll_interval = int(config.get("poll_interval_minutes", 5)) * 60
        state_file = config.get("state_file", "email_ingestor_state.json")
        state = load_state(state_file)

        webhook_queue = []
        start_webhook(config, webhook_queue)

        while True:
            emails = []
            if mode == "imap":
                emails = imap_fetch(config, state)
            elif mode == "webhook":
                # Process any emails received via webhook
                while webhook_queue:
                    emails.append(webhook_queue.pop(0))
            else:
                _post(f"Invalid mode: {mode}", "error")
                break

            for msg in emails:
                if "subject" not in msg:
                    # It's a raw IMAP dict; we have parsed fields
                    pass
                # Determine categories
                cats = categorize_email(msg, config)
                amounts = []
                if config.get("parsing", {}).get("extract_amounts", True):
                    body = msg.get("body", "")
                    amounts = extract_amounts(msg.get("subject", "") + " " + body)
                payload = {
                    "source": "email",
                    "subject": msg.get("subject", ""),
                    "from": msg.get("from", ""),
                    "date": msg.get("date", ""),
                    "body_snippet": msg.get("body", "")[:200],
                    "categories": cats,
                    "amounts": amounts,
                    "uid": msg.get("uid"),
                    "message_id": msg.get("message_id", "")
                }
                summary = f"Email: {payload['subject']} (from {payload['from']})"
                if cats:
                    summary += f" [{', '.join(cats)}]"
                if amounts:
                    summary += f" amounts: {amounts}"
                level = "info" if cats else "info"
                _post(summary, level, payload)

            save_state(state_file, state)
            _heartbeat()
            time.sleep(poll_interval)

if __name__ == "__main__":
    main()
