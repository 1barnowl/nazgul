#!/usr/bin/env python3
"""
automated_email_bot.py — Automated Email Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Autonomously sends and replies to emails using
IMAP/SMTP.  Can use LLM (OpenAI) for contextual
replies or fall back to hard‑coded templates.
Also exposes an HTTP API for other bots to
trigger sends.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `automated_email_config.json` in the same directory:

{
  "email": {
    "imap_host": "imap.example.com",
    "imap_port": 993,
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
    "username": "user@example.com",
    "password": "your_password",
    "use_starttls": true,
    "poll_interval_seconds": 60
  },
  "reply": {
    "enabled": true,
    "template": "Hi {sender_name}, thanks for your email! We'll get back to you soon.",
    "llm": null,                           // optional: { "provider": "openai", "api_key": "sk-...", "model": "gpt-4o-mini" }
    "max_replies_per_run": 5,
    "ignore_senders": ["no-reply@example.com"]
  },
  "http_api": {
    "enabled": true,
    "port": 9590
  },
  "state_file": "automated_email_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import imaplib
import smtplib
import email
import time
import threading
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "automated_email_bot"
BOT_NAME = "Automated Email Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "automated_email_config.json"
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
        return {"replied_message_ids": [], "last_uid": None}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── IMAP / SMTP helpers ──────────────────────────────────────────
def connect_imap(config: dict) -> Optional[imaplib.IMAP4_SSL]:
    try:
        conn = imaplib.IMAP4_SSL(config["imap_host"], int(config.get("imap_port", 993)))
        conn.login(config["username"], config["password"])
        conn.select("INBOX", readonly=False)
        return conn
    except Exception as e:
        _post(f"IMAP connection failed: {e}", "error")
        return None

def fetch_new_emails(conn: imaplib.IMAP4_SSL, last_uid: Optional[int]) -> List[dict]:
    """Fetch unseen emails since the last UID, return parsed dicts."""
    if last_uid is not None:
        result, data = conn.uid("search", None, f"UID {int(last_uid)+1}:*")
    else:
        result, data = conn.uid("search", None, "UNSEEN")
    if result != "OK" or not data[0]:
        return []
    uids = data[0].split()
    emails = []
    for uid in uids[:20]:  # limit per poll
        try:
            resp, msg_data = conn.uid("fetch", uid, "(RFC822)")
            if resp != "OK":
                continue
            raw_email = msg_data[0][1]
            parsed = email.message_from_bytes(raw_email)
            subject = parsed.get("Subject", "").strip()
            from_addr = parsed.get("From", "").strip()
            # Extract plain text body
            body = ""
            if parsed.is_multipart():
                for part in parsed.walk():
                    content_type = part.get_content_type()
                    if content_type == "text/plain" and "attachment" not in str(part.get("Content-Disposition", "")):
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(errors="replace").strip()
                            break
            else:
                payload = parsed.get_payload(decode=True)
                if payload:
                    body = payload.decode(errors="replace").strip()
            emails.append({
                "uid": int(uid),
                "subject": subject,
                "from": from_addr,
                "body": body,
                "date": parsed.get("Date", ""),
                "message_id": parsed.get("Message-ID", "")
            })
        except Exception as e:
            _post(f"Failed to parse email UID {uid}: {e}", "warning")
    return emails

def send_email_via_smtp(config: dict, to: str, subject: str, body: str,
                        from_addr: Optional[str] = None) -> bool:
    """Send an email via SMTP."""
    smtp_host = config["smtp_host"]
    smtp_port = int(config.get("smtp_port", 587))
    username = config["username"]
    password = config["password"]
    use_starttls = config.get("use_starttls", True)
    from_addr = from_addr or username

    msg = MIMEMultipart()
    msg["From"] = from_addr
    msg["To"] = to
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain"))

    try:
        if smtp_port == 465:
            # SSL
            server = smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15)
        else:
            server = smtplib.SMTP(smtp_host, smtp_port, timeout=15)
            if use_starttls:
                server.starttls()
        server.login(username, password)
        server.sendmail(from_addr, [to], msg.as_string())
        server.quit()
        return True
    except Exception as e:
        _post(f"SMTP send failed: {e}", "error")
        return False

# ── LLM reply generation ─────────────────────────────────────────
def generate_reply_with_llm(email_body: str, llm_config: dict) -> Optional[str]:
    """Generate a reply using OpenAI. Returns None if not configured or fails."""
    if not llm_config or not llm_config.get("api_key"):
        return None
    provider = llm_config.get("provider", "openai")
    if provider != "openai":
        return None
    endpoint = llm_config.get("endpoint") or "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {llm_config['api_key']}",
        "Content-Type": "application/json"
    }
    data = {
        "model": llm_config.get("model", "gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": "You are a helpful assistant replying to an email. Write a brief, professional response."},
            {"role": "user", "content": f"Email content: {email_body}\n\nWrite a reply:"}
        ],
        "temperature": 0.7,
        "max_tokens": 300
    }
    try:
        resp = requests.post(endpoint, json=data, headers=headers, timeout=20)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            _post(f"LLM reply error: {resp.status_code}", "warning")
            return None
    except Exception as e:
        _post(f"LLM request failed: {e}", "error")
        return None

# ── Incoming email processing ────────────────────────────────────
def process_incoming(config: dict, state: dict):
    """Fetch new emails and auto‑reply if enabled."""
    if not config.get("reply", {}).get("enabled"):
        return
    imap_conn = connect_imap(config["email"])
    if not imap_conn:
        return

    last_uid = state.get("last_uid")
    new_emails = fetch_new_emails(imap_conn, last_uid)
    if not new_emails:
        imap_conn.logout()
        return

    reply_cfg = config["reply"]
    template = reply_cfg.get("template", "Thanks for your email!")
    llm_cfg = reply_cfg.get("llm")
    max_replies = int(reply_cfg.get("max_replies_per_run", 5))
    ignore_senders = set(reply_cfg.get("ignore_senders", []))
    replied_ids = set(state.get("replied_message_ids", []))

    replied_count = 0
    for mail in new_emails:
        if replied_count >= max_replies:
            break
        message_id = mail["message_id"]
        if message_id and message_id in replied_ids:
            continue
        from_addr = mail["from"]
        # Extract sender email address from "Name <email>" format
        from_email = email.utils.parseaddr(from_addr)[1]
        if from_email in ignore_senders:
            continue
        # Extract sender name for template
        sender_name = email.utils.parseaddr(from_addr)[0] or "there"

        # Build reply body
        if llm_cfg and llm_cfg.get("api_key"):
            reply_body = generate_reply_with_llm(mail["body"], llm_cfg)
            if not reply_body:
                reply_body = template.replace("{sender_name}", sender_name)
        else:
            reply_body = template.replace("{sender_name}", sender_name)

        # Send reply
        subject = "Re: " + mail["subject"] if not mail["subject"].startswith("Re:") else mail["subject"]
        success = send_email_via_smtp(config["email"], from_email, subject, reply_body)
        if success:
            _post(f"Replied to {from_email}", "info", {"to": from_email, "subject": subject})
            replied_ids.add(message_id)
            replied_count += 1
            # Mark as replied in state
            if message_id:
                state.setdefault("replied_message_ids", []).append(message_id)
            time.sleep(1)  # polite rate limit
        else:
            _post(f"Failed to reply to {from_email}", "error")

        # Update last UID in state (set to the highest UID processed)
        if mail["uid"] > (state.get("last_uid") or 0):
            state["last_uid"] = mail["uid"]

    # Trim replied message IDs list
    state["replied_message_ids"] = list(set(state.get("replied_message_ids", [])))[-500:]
    imap_conn.logout()

# ── HTTP API for sending emails on behalf of other bots ──────────
class EmailAPIHandler(BaseHTTPRequestHandler):
    config: dict = None

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
                    self._respond(400, {"error": "Missing 'to' or 'body'"})
                    return
                success = send_email_via_smtp(self.config["email"], to, subject, text, from_addr)
                if success:
                    _post(f"HTTP API: sent email to {to}", "info")
                    self._respond(200, {"status": "sent"})
                else:
                    self._respond(500, {"error": "SMTP send failed"})
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

def start_http_api(config: dict):
    if not config.get("http_api", {}).get("enabled", True):
        return
    port = int(config["http_api"].get("port", 9590))
    EmailAPIHandler.config = config
    server = HTTPServer(("0.0.0.0", port), EmailAPIHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Email API listening on port {port}", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Automated Email Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "automated_email_state.json")
        state = load_state(state_file)

        # Start HTTP API (non‑blocking)
        start_http_api(config)

        poll_seconds = int(config.get("email", {}).get("poll_interval_seconds", 60))

        # Main loop
        while True:
            try:
                process_incoming(config, state)
            except Exception as e:
                _post(f"Error processing incoming emails: {e}", "error")
            save_state(state_file, state)
            _heartbeat()
            time.sleep(poll_seconds)

if __name__ == "__main__":
    main()
