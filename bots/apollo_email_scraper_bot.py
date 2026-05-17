#!/usr/bin/env python3
"""
apollo_email_scraper_bot.py — Apollo.io Email Scraper Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Searches Apollo.io for contacts matching criteria, extracts
email addresses, stores them, and optionally pushes them to
the Email Sender Bot via its HTTP API.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `apollo_email_scraper_config.json` in the same directory:

{
  "apollo": {
    "api_key": "YOUR_APOLLO_API_KEY",
    "base_url": "https://api.apollo.io/v1/"
  },
  "search": {
    "q_keywords": "growth hacker",               // free‑form search
    "person_titles": ["CEO", "Founder"],
    "organization_locations": ["US"],
    "per_page": 10,
    "max_pages": 5
  },
  "output": {
    "file": "apollo_emails.json",
    "format": "json",                            // json or csv
    "push_to_email_sender": false,
    "email_sender_url": "http://localhost:9595/send"
  },
  "state_file": "apollo_email_scraper_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 1440                  // daily
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "apollo_email_scraper_bot"
BOT_NAME = "Apollo Email Scraper"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "apollo_email_scraper_config.json"
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
        return {"last_run": None, "last_page": 0, "fetched_email_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Apollo API ───────────────────────────────────────────────────
APOLLO_API = "https://api.apollo.io/v1/"

def apollo_search_contacts(api_key: str, search_cfg: dict, page: int = 1,
                           per_page: int = 10) -> Optional[dict]:
    """
    Perform a mixed‑search for contacts. Returns the JSON response or None.
    """
    headers = {
        "Content-Type": "application/json",
        "Cache-Control": "no-cache"
    }
    params = {
        "api_key": api_key,
        "page": page,
        "per_page": per_page,
        "q_keywords": search_cfg.get("q_keywords", ""),
        "person_titles[]": search_cfg.get("person_titles", []),
        "organization_locations[]": search_cfg.get("organization_locations", []),
    }
    # Remove empty arrays
    params = {k: v for k, v in params.items() if v}

    try:
        resp = requests.post(f"{APOLLO_API}mixed_people/search", headers=headers, data=json.dumps(params), timeout=20)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"Apollo API error {resp.status_code}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Apollo API request failed: {e}", "error")
        return None

def extract_emails(contacts: List[dict]) -> List[dict]:
    """Return list of {email, first_name, last_name, title, company} from contacts."""
    extracted = []
    for contact in contacts:
        email = contact.get("email")
        if not email:
            continue
        first_name = contact.get("first_name", "")
        last_name = contact.get("last_name", "")
        title = contact.get("title", "")
        company = contact.get("organization", {}).get("name", "")
        extracted.append({
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "title": title,
            "company": company
        })
    return extracted

def save_emails(emails: List[dict], output_cfg: dict, new_only: bool = True) -> int:
    """
    Append emails to the output file, optionally deduplicating.
    Returns number of newly added emails.
    """
    file_path = output_cfg.get("file", "apollo_emails.json")
    format_type = output_cfg.get("format", "json")
    existing_emails = []
    existing_emails_set = set()

    if new_only and os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                if format_type == "json":
                    existing_emails = json.load(f)
                    existing_emails_set = {e["email"] for e in existing_emails if "email" in e}
        except Exception:
            pass  # start fresh

    new_emails = []
    for email_entry in emails:
        if email_entry["email"] not in existing_emails_set:
            new_emails.append(email_entry)
            existing_emails_set.add(email_entry["email"])

    if not new_emails:
        return 0

    if format_type == "json":
        existing_emails.extend(new_emails)
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(existing_emails, f, indent=2)
    elif format_type == "csv":
        import csv
        file_exists = os.path.exists(file_path)
        with open(file_path, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["email", "first_name", "last_name", "title", "company"])
            if not file_exists:
                writer.writeheader()
            writer.writerows(new_emails)
    return len(new_emails)

def push_to_email_sender(emails: List[dict], email_sender_url: str) -> int:
    """Send each email address to the Email Sender Bot as a recipient."""
    count = 0
    for entry in emails:
        try:
            payload = {
                "to": entry["email"],
                "subject": "Automated outreach",
                "body": f"Hello {entry['first_name']},\n\nWe think you might be interested in our service."
            }
            resp = requests.post(email_sender_url, json=payload, timeout=5)
            if resp.status_code in (200, 201):
                count += 1
        except Exception as e:
            _post(f"Failed to push email to sender: {e}", "warning")
    return count

# ── Main scraping routine ────────────────────────────────────────
def scrape_emails(config: dict, state: dict):
    apollo_cfg = config["apollo"]
    search_cfg = config["search"]
    output_cfg = config["output"]

    api_key = apollo_cfg["api_key"]
    per_page = int(search_cfg.get("per_page", 10))
    max_pages = int(search_cfg.get("max_pages", 5))

    all_emails = []
    page = state.get("last_page", 0) + 1  # start where we left off
    for page_num in range(page, page + max_pages):
        resp = apollo_search_contacts(api_key, search_cfg, page=page_num, per_page=per_page)
        if not resp:
            break
        contacts = resp.get("people", []) or resp.get("contacts", [])
        if not contacts:
            break
        emails = extract_emails(contacts)
        all_emails.extend(emails)
        # Save progress
        state["last_page"] = page_num
        # Rate limiting
        time.sleep(1.5)
        if len(contacts) < per_page:
            break  # last page

    # Save to file
    if all_emails:
        new_count = save_emails(all_emails, output_cfg, new_only=True)
        _post(f"Scraped {len(all_emails)} emails, {new_count} new", "info", {"total": len(all_emails), "new": new_count})

        # Optionally push to email sender
        if output_cfg.get("push_to_email_sender"):
            email_sender_url = output_cfg.get("email_sender_url")
            if email_sender_url:
                pushed = push_to_email_sender(all_emails, email_sender_url)
                _post(f"Pushed {pushed} emails to Email Sender Bot", "info")
    else:
        _post("No new emails found in this run", "info")

    # Reset pagination if we completed all pages
    state["last_page"] = 0
    state["last_run"] = datetime.now(timezone.utc).isoformat()

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Apollo Email Scraper Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "apollo_email_scraper_state.json")
        state = load_state(state_file)

        # Check if it's time to run
        poll_minutes = int(config.get("poll_interval_minutes", 1440))
        last_run_str = state.get("last_run")
        if last_run_str:
            last_run = datetime.fromisoformat(last_run_str)
            if datetime.now(timezone.utc) - last_run < timedelta(minutes=poll_minutes):
                _post("Poll interval not reached, waiting", "info")
                _heartbeat()
                time.sleep(60)
                continue

        scrape_emails(config, state)
        save_state(state_file, state)

        _heartbeat()
        # Wait until next cycle (but we'll loop soon)
        time.sleep(60)

if __name__ == "__main__":
    main()
