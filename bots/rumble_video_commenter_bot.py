#!/usr/bin/env python3
"""
rumble_video_commenter_bot.py — Rumble Video Commenter Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Posts comments on popular Rumble videos aligned with your
niche. Uses Playwright to automate the browser and interact
with the Rumble website. No official API exists for
commenting, so web automation is the real‑world approach.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install playwright requests
    playwright install chromium

Configuration
─────────────
Place `rumble_commenter_config.json` in the same directory:

{
  "rumble": {
    "username": "your_email_or_username",
    "password": "your_password",
    "headless": true,
    "login_url": "https://rumble.com/login.php",
    "home_url": "https://rumble.com/"
  },
  "commenting": {
    "enabled": true,
    "keywords": ["crypto", "finance", "bitcoin", "stock market"],
    "channel_urls": [
      "https://rumble.com/c/FinanceNews"
    ],
    "comment_template": "Great content! I've been covering similar topics on my channel. Keep it up!",
    "max_comments_per_run": 3,
    "dry_run": false,
    "check_interval_minutes": 60
  },
  "state_file": "rumble_commenter_state.json",
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
BOT_ID = "rumble_video_commenter_bot"
BOT_NAME = "Rumble Video Commenter"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "rumble_commenter_config.json"
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
        return {"commented_videos": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Playwright automation ────────────────────────────────────────
def rumble_login(page: Page, config: dict) -> bool:
    """Log into Rumble using the provided credentials."""
    try:
        page.goto(config["rumble"]["login_url"], wait_until="networkidle")
        page.fill('input[name="username"]', config["rumble"]["username"])
        page.fill('input[name="password"]', config["rumble"]["password"])
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        # Check if login succeeded by looking for an element that appears when logged in (e.g., user avatar)
        if page.query_selector('img.avatar') or page.query_selector('a[href*="/user/"]'):
            _post("Successfully logged into Rumble", "info")
            return True
        else:
            _post("Rumble login may have failed – user avatar not found", "warning")
            # attempt to continue anyway
            return True
    except Exception as e:
        _post(f"Rumble login error: {e}", "error")
        return False

def find_videos(page: Page, keywords: List[str], channel_urls: List[str], max_videos: int = 10) -> List[str]:
    """
    Search for videos or load channel pages and return a list of video URLs.
    """
    video_urls = set()
    # From channel pages
    for ch_url in channel_urls:
        try:
            page.goto(ch_url, wait_until="networkidle")
            # Scroll to load more videos
            for _ in range(3):
                page.evaluate("window.scrollBy(0, 1000)")
                time.sleep(1)
            # Grab video links
            links = page.query_selector_all('a[href*="/v"]')
            for link in links:
                href = link.get_attribute("href")
                if href and "/v" in href:
                    full_url = "https://rumble.com" + href if href.startswith("/") else href
                    video_urls.add(full_url)
        except Exception as e:
            _post(f"Error fetching channel {ch_url}: {e}", "error")

    # From keyword searches
    for kw in keywords:
        try:
            search_url = f"https://rumble.com/search/video?q={kw}"
            page.goto(search_url, wait_until="networkidle")
            page.wait_for_selector('div.video-listing', timeout=5000)
            for _ in range(2):
                page.evaluate("window.scrollBy(0, 800)")
                time.sleep(1)
            links = page.query_selector_all('a[href*="/v"]')
            for link in links:
                href = link.get_attribute("href")
                if href and "/v" in href:
                    full_url = "https://rumble.com" + href if href.startswith("/") else href
                    video_urls.add(full_url)
        except Exception as e:
            _post(f"Error searching keyword '{kw}': {e}", "error")

    # Limit to max_videos
    return list(video_urls)[:max_videos]

def comment_on_video(page: Page, video_url: str, comment_text: str) -> bool:
    """Navigate to a video, scroll to comment section, and post the comment."""
    try:
        page.goto(video_url, wait_until="networkidle")
        # Wait for comment box to be available
        page.wait_for_selector('textarea#comment-text', timeout=5000)
        # Fill and submit
        page.fill('textarea#comment-text', comment_text)
        page.click('button[type="submit"]')
        # Wait for comment to appear
        page.wait_for_timeout(2000)
        # Simple check: look for the comment just posted? Not trivial, assume success if no error.
        _post(f"Comment posted on {video_url}", "info")
        return True
    except Exception as e:
        _post(f"Failed to comment on {video_url}: {e}", "error")
        return False

def process_commenting(config: dict, state: dict):
    """Log in, find videos, and post comments."""
    if not config.get("commenting", {}).get("enabled", False):
        return

    dry_run = config["commenting"].get("dry_run", False)
    max_comments = int(config["commenting"].get("max_comments_per_run", 3))
    keywords = config["commenting"].get("keywords", [])
    channel_urls = config["commenting"].get("channel_urls", [])
    comment_template = config["commenting"].get("comment_template", "")
    if not comment_template:
        _post("Comment template is empty", "error")
        return

    already_commented = set(state.get("commented_videos", []))

    with sync_playwright() as p:
        headless = config["rumble"].get("headless", True)
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        if not rumble_login(page, config):
            browser.close()
            return

        # Find videos
        videos = find_videos(page, keywords, channel_urls)
        _post(f"Found {len(videos)} potential videos", "info")

        count = 0
        for vid_url in videos:
            if count >= max_comments:
                break
            if vid_url in already_commented:
                continue
            if dry_run:
                _post(f"[DRY RUN] Would comment on {vid_url}: {comment_template}", "info")
                already_commented.add(vid_url)
                count += 1
                continue
            success = comment_on_video(page, vid_url, comment_template)
            if success:
                already_commented.add(vid_url)
                count += 1
                time.sleep(2)  # be polite
            else:
                # Don't add to commented if failed, so we can retry later
                pass

        state["commented_videos"] = list(already_commented)[-1000:]
        browser.close()

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Rumble Video Commenter Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "rumble_commenter_state.json")
        state = load_state(state_file)

        process_commenting(config, state)

        save_state(state_file, state)

        check_min = int(config.get("commenting", {}).get("check_interval_minutes", 60)) * 60
        _heartbeat()
        time.sleep(check_min)

if __name__ == "__main__":
    main()
