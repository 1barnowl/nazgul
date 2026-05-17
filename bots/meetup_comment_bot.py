#!/usr/bin/env python3
"""
meetup_comment_bot.py — Meetup Comment Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Automatically logs into Meetup, searches for events
matching keywords, and leaves constructive comments
on event pages to network and promote your brand.

Eventbrite comments are not supported because
Eventbrite’s API / website does not allow automated
commenting – this bot focuses on Meetup.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install playwright requests
    playwright install chromium

Configuration
─────────────
Place `meetup_comment_config.json` in the same directory:

{
  "meetup": {
    "email": "your_email@example.com",
    "password": "your_password",
    "headless": true,
    "login_url": "https://www.meetup.com/login/"
  },
  "commenting": {
    "enabled": true,
    "keywords": ["python", "data science", "machine learning"],
    "comment_template": "Sounds like a great event! I've been following this space and wrote a resource that might be relevant: {link}",
    "link": "https://your-site.com",
    "max_comments_per_run": 3,
    "check_interval_minutes": 60,
    "dry_run": false
  },
  "state_file": "meetup_comment_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from playwright.sync_api import sync_playwright, Page, Browser

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "meetup_comment_bot"
BOT_NAME = "Meetup Comment"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "meetup_comment_config.json"
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
        return {"commented_event_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Playwright automation ────────────────────────────────────────
def meetup_login(page: Page, email: str, password: str) -> bool:
    """Log into Meetup using the login form."""
    try:
        page.goto("https://www.meetup.com/login/", wait_until="networkidle")
        # Accept cookies if prompted (optional)
        # Fill credentials
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        # Check if login succeeded (e.g., user avatar appears)
        if page.query_selector('a[href*="/me/"]') or "login" not in page.url:
            _post("Logged into Meetup successfully", "info")
            return True
        else:
            _post("Meetup login may have failed", "warning")
            # attempt to continue
            return True
    except Exception as e:
        _post(f"Meetup login error: {e}", "error")
        return False

def search_events(page: Page, keyword: str, max_events: int = 5) -> List[dict]:
    """Search for upcoming events matching the keyword. Return list of event URLs/IDs."""
    page.goto(f"https://www.meetup.com/find/?keywords={keyword}&source=EVENTS", wait_until="networkidle")
    # Scroll to load more
    for _ in range(3):
        page.evaluate("window.scrollBy(0, 1000)")
        time.sleep(1)
    # Get event cards
    event_cards = page.query_selector_all('a[href*="/events/"]')
    events = []
    for card in event_cards:
        href = card.get_attribute("href")
        if href and "/events/" in href and href not in [e["url"] for e in events]:
            events.append({"url": href if href.startswith("http") else "https://www.meetup.com" + href})
            if len(events) >= max_events:
                break
    return events

def comment_on_event(page: Page, event_url: str, comment_text: str) -> bool:
    """Navigate to an event page and post a comment in the 'Comments' section."""
    try:
        page.goto(event_url, wait_until="networkidle")
        # Scroll to comments area (if not visible)
        page.evaluate("window.scrollBy(0, 800)")
        # Look for the comment input area (Meetup uses a specific editor)
        comment_box = page.query_selector('div[data-testid="comment-input"] textarea, div[role="textbox"]')
        if not comment_box:
            # Try alternative selectors
            comment_box = page.query_selector('div.public-DraftEditor-content')
        if not comment_box:
            _post(f"Could not find comment input on {event_url}", "error")
            return False
        comment_box.click()
        comment_box.type(comment_text)
        time.sleep(1)
        # Click "Post" or "Submit" button
        submit_btn = page.query_selector('button[data-testid="submit-comment"], button:has-text("Post")')
        if submit_btn:
            submit_btn.click()
            time.sleep(2)
            return True
        else:
            # Try pressing Enter? Not reliable.
            return False
    except Exception as e:
        _post(f"Comment error on {event_url}: {e}", "error")
        return False

def process_comments(config: dict, state: dict):
    """Main commenting routine."""
    if not config.get("commenting", {}).get("enabled"):
        return
    meetup_cfg = config["meetup"]
    commenting_cfg = config["commenting"]
    dry_run = commenting_cfg.get("dry_run", False)
    max_comments = int(commenting_cfg.get("max_comments_per_run", 3))
    keywords = commenting_cfg.get("keywords", [])
    comment_template = commenting_cfg.get("comment_template", "")
    link = commenting_cfg.get("link", "")
    already_commented = set(state.get("commented_event_ids", []))

    if not keywords or not comment_template:
        _post("Keywords or comment template missing", "warning")
        return

    with sync_playwright() as p:
        headless = meetup_cfg.get("headless", True)
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        if not meetup_login(page, meetup_cfg["email"], meetup_cfg["password"]):
            browser.close()
            return

        count = 0
        for keyword in keywords:
            events = search_events(page, keyword)
            for event in events:
                event_url = event["url"]
                if event_url in already_commented:
                    continue
                comment_text = comment_template.replace("{link}", link)
                if dry_run:
                    _post(f"[DRY] Would comment on {event_url}: {comment_text[:60]}...", "info")
                    already_commented.add(event_url)
                    count += 1
                    if count >= max_comments:
                        break
                    continue
                success = comment_on_event(page, event_url, comment_text)
                if success:
                    _post(f"Commented on event: {event_url}", "info")
                    already_commented.add(event_url)
                    count += 1
                    time.sleep(5)  # be polite
                else:
                    _post(f"Failed to comment on {event_url}", "error")
                if count >= max_comments:
                    break
            if count >= max_comments:
                break

        state["commented_event_ids"] = list(already_commented)[-500:]
        browser.close()

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Meetup Comment Bot online")
    _post("Note: Eventbrite comments are not supported due to lack of public API / automation capability.", "info")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "meetup_comment_state.json")
        state = load_state(state_file)

        process_comments(config, state)

        save_state(state_file, state)

        check_min = int(config.get("commenting", {}).get("check_interval_minutes", 60))
        _heartbeat()
        time.sleep(check_min * 60)

if __name__ == "__main__":
    main()
