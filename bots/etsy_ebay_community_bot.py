#!/usr/bin/env python3
"""
etsy_ebay_community_bot.py — Etsy / eBay Community Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Participates in seller forums on Etsy and eBay by leaving
helpful advice comments that naturally mention your external
tool. Uses Playwright because no public forum API exists.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install playwright requests
    playwright install chromium

Configuration
─────────────
Place `etsy_ebay_community_config.json` in the same directory:

{
  "etsy": {
    "enabled": true,
    "email": "your_etsy_email",
    "password": "your_etsy_password",
    "headless": true,
    "forum_url": "https://community.etsy.com/",
    "keywords": ["sales", "traffic", "marketing", "SEO", "shipping"],
    "comment_template": "I've been using a tool that helps with {keyword}. It's made a huge difference. You can check it out here: {link}",
    "link": "https://your-tool.com",
    "max_comments_per_run": 2,
    "cooldown_hours": 12
  },
  "ebay": {
    "enabled": true,
    "email": "your_ebay_email",
    "password": "your_ebay_password",
    "headless": true,
    "forum_url": "https://community.ebay.com/",
    "keywords": ["listing", "sold", "visibility", "feedback", "shipping"],
    "comment_template": "I've found a great solution for that: {link} – it's really helped my store.",
    "link": "https://your-tool.com",
    "max_comments_per_run": 2,
    "cooldown_hours": 12
  },
  "state_file": "etsy_ebay_community_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 360
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
BOT_ID = "etsy_ebay_community_bot"
BOT_NAME = "Etsy / eBay Community"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "etsy_ebay_community_config.json"
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
        return {
            "etsy_last_run": None,
            "ebay_last_run": None,
            "etsy_commented_threads": [],
            "ebay_commented_threads": []
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Generic forum commenter (Playwright) ─────────────────────────
def login(page: Page, platform: str, email: str, password: str, login_url: str) -> bool:
    """Log into Etsy or eBay community using given selectors."""
    try:
        page.goto(login_url, wait_until="networkidle")
        # Click "Sign In" link if present
        signin_btn = page.query_selector('a:has-text("Sign In"), a:has-text("Log In"), button:has-text("Sign In")')
        if signin_btn:
            signin_btn.click()
            page.wait_for_load_state("networkidle")
        # Fill email/username
        page.fill('input[name="email"], input[name="username"], input[type="email"]', email)
        page.fill('input[name="password"], input[type="password"]', password)
        page.click('button[type="submit"], input[type="submit"], button:has-text("Sign In")')
        page.wait_for_load_state("networkidle")
        # Verify success (look for account menu)
        if page.query_selector('a[href*="profile"], a[href*="account"], .globalHeader__account']):
            _post(f"Logged into {platform} community", "info")
            return True
        _post(f"{platform} login may have failed", "warning")
        return True  # continue anyway
    except Exception as e:
        _post(f"{platform} login error: {e}", "error")
        return False

def find_forum_threads(page: Page, keywords: List[str], max_threads: int) -> List[str]:
    """Search the current forum page for thread links matching keywords."""
    thread_urls = []
    for keyword in keywords:
        # Search within the page (some forums have search box)
        search_input = page.query_selector('input[name="query"], input[placeholder*="Search"]')
        if search_input:
            search_input.fill(keyword)
            search_input.press("Enter")
            page.wait_for_timeout(2000)
        # Collect thread links
        links = page.query_selector_all('a[href*="/t/"], a[href*="thread"], a[data-testid="thread-link"]')
        for link in links:
            href = link.get_attribute("href")
            if href:
                full_url = href if href.startswith("http") else page.url.split("/")[0] + "//" + page.url.split("/")[2] + href
                if full_url not in thread_urls:
                    thread_urls.append(full_url)
                    if len(thread_urls) >= max_threads:
                        break
        if len(thread_urls) >= max_threads:
            break
    return thread_urls

def post_comment(page: Page, thread_url: str, comment_text: str) -> bool:
    """Navigate to a thread and post a comment."""
    try:
        page.goto(thread_url, wait_until="networkidle")
        # Scroll to reply area
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        # Look for the reply editor
        editor = page.query_selector('div[contenteditable="true"], textarea, .ql-editor')
        if not editor:
            # Click "Reply" or "Post a Reply" button first
            reply_btn = page.query_selector('button:has-text("Reply"), a:has-text("Reply")')
            if reply_btn:
                reply_btn.click()
                page.wait_for_timeout(1000)
                editor = page.query_selector('div[contenteditable="true"], textarea')
        if not editor:
            _post(f"No comment editor on {thread_url}", "error")
            return False
        editor.click()
        editor.type(comment_text)
        time.sleep(0.5)
        # Submit
        submit = page.query_selector('button[type="submit"], button:has-text("Post"), button:has-text("Submit")')
        if submit:
            submit.click()
            page.wait_for_timeout(2000)
            return True
        return False
    except Exception as e:
        _post(f"Comment error on {thread_url}: {e}", "error")
        return False

def process_forum(config: dict, platform: str, state_key: str, state: dict):
    """Generic routine for a single platform."""
    plat_cfg = config.get(platform, {})
    if not plat_cfg.get("enabled"):
        return
    # Rate limiting
    last_run_str = state.get(f"{platform}_last_run")
    cooldown_hours = float(plat_cfg.get("cooldown_hours", 12))
    if last_run_str:
        last_run = datetime.fromisoformat(last_run_str)
        if datetime.now(timezone.utc) - last_run < timedelta(hours=cooldown_hours):
            _post(f"{platform}: cooldown active, skipping", "info")
            return

    email = plat_cfg.get("email")
    password = plat_cfg.get("password")
    headless = plat_cfg.get("headless", True)
    forum_url = plat_cfg.get("forum_url", "")
    keywords = plat_cfg.get("keywords", [])
    template = plat_cfg.get("comment_template", "")
    link = plat_cfg.get("link", "")
    max_comments = int(plat_cfg.get("max_comments_per_run", 2))
    if not all([email, password, keywords, template]):
        _post(f"{platform}: missing credentials or keywords", "warning")
        return

    already_commented = set(state.get(f"{platform}_commented_threads", []))
    comments_posted = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        # Login
        login_url = forum_url.rstrip("/") + "/login" if platform == "etsy" else forum_url.rstrip("/") + "/t5/help/faqpage"
        # Actually eBay community uses a different login. We'll use the generic URL.
        if not login(page, platform, email, password, forum_url):
            browser.close()
            return

        # Navigate to the main discussion area
        page.goto(forum_url, wait_until="networkidle")
        # Etsy: discussion boards URL might be /forums. We'll use the home.
        # For simplicity, we assume the forum URL provided is the list of recent topics.

        thread_urls = find_forum_threads(page, keywords, max_threads=max_comments * 3)
        for url in thread_urls:
            if comments_posted >= max_comments:
                break
            if url in already_commented:
                continue
            # Pick a keyword that appears in the thread title (not implemented) – we'll just use the first keyword
            chosen_kw = keywords[0]
            comment = template.replace("{keyword}", chosen_kw).replace("{link}", link)
            success = post_comment(page, url, comment)
            if success:
                _post(f"{platform}: commented on {url}", "info")
                already_commented.add(url)
                comments_posted += 1
                time.sleep(3)  # polite
            else:
                _post(f"{platform}: failed to comment on {url}", "error")

        browser.close()

    state[f"{platform}_commented_threads"] = list(already_commented)[-500:]
    state[f"{platform}_last_run"] = datetime.now(timezone.utc).isoformat()

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Etsy / eBay Community Bot online")
    _post("Note: Forum automation via browser may violate platform terms. Use responsibly.", "warning")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "etsy_ebay_community_state.json")
        state = load_state(state_file)

        process_forum(config, "etsy", "etsy", state)
        process_forum(config, "ebay", "ebay", state)

        save_state(state_file, state)

        poll_min = int(config.get("poll_interval_minutes", 360))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
