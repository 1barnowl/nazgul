```python
#!/usr/bin/env python3
"""
amazon_review_comment_bot.py — Amazon Book / Product Review Comment Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Leaves helpful comments on Amazon product or book reviews
using Playwright to automate a logged‑in browser session.
Because Amazon has no public comment API, this bot relies
on real browser interaction.  It respects dry‑run mode and
state to avoid duplicate comments.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install playwright requests
    playwright install chromium

Configuration
─────────────
Place `amazon_review_comment_config.json` in the same directory:

{
  "amazon": {
    "email": "your_amazon_email",
    "password": "your_amazon_password",
    "headless": true
  },
  "commenting": {
    "enabled": true,
    "products": [
      {
        "asin": "B08N5WRWNW",
        "name": "Example Product"
      }
    ],
    "keywords": ["python", "automation"],
    "comment_template": "Great review! I've found a tool that complements this perfectly: {link}",
    "link": "https://your-product.com",
    "max_comments_per_run": 2,
    "check_interval_minutes": 360,
    "dry_run": false
  },
  "state_file": "amazon_review_comment_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from playwright.sync_api import sync_playwright, Page, Browser

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "amazon_review_comment_bot"
BOT_NAME = "Amazon Review Comment"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "amazon_review_comment_config.json"
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
        return {"commented_review_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Playwright automation ────────────────────────────────────────
def amazon_login(page: Page, email: str, password: str) -> bool:
    """Log into Amazon using email/password. May fail if CAPTCHA appears."""
    try:
        page.goto("https://www.amazon.com/ap/signin", wait_until="networkidle")
        # Fill email
        page.fill('input[type="email"]', email)
        page.click('input#continue')
        page.wait_for_load_state("networkidle")
        # Fill password
        page.fill('input[type="password"]', password)
        page.click('input#signInSubmit')
        page.wait_for_load_state("networkidle")
        # Check for OTP challenge (if any)
        if "ap/cvf" in page.url or "ap/mfa" in page.url:
            _post("Amazon login requires OTP – cannot automate further", "error")
            return False
        # If we see the Amazon logo or account link, we're logged in
        if page.query_selector("#nav-link-accountList") is not None:
            _post("Logged into Amazon", "info")
            return True
        else:
            _post("Amazon login may have failed (CAPTCHA?)", "warning")
            # Attempt to continue anyway
            return True
    except Exception as e:
        _post(f"Amazon login error: {e}", "error")
        return False

def get_review_urls(page: Page, product_url: str, max_reviews: int = 5) -> List[str]:
    """Navigate to a product's review page and extract individual review permalinks."""
    page.goto(product_url, wait_until="networkidle")
    # Look for review permalink elements – they often have a "data-hook" attribute or are inside a specific container.
    review_links = page.query_selector_all('a[data-hook="review-title"]')
    urls = []
    for link in review_links:
        href = link.get_attribute("href")
        if href and "/product-reviews/" in href:
            full_url = "https://www.amazon.com" + href if href.startswith("/") else href
            # The link often goes to the specific review anchor; we can comment there.
            urls.append(full_url)
            if len(urls) >= max_reviews:
                break
    # Fallback: search for any link with "/review/" in href
    if not urls:
        all_links = page.query_selector_all('a[href*="/review/"]')
        for link in all_links:
            href = link.get_attribute("href")
            if href:
                full_url = "https://www.amazon.com" + href if href.startswith("/") else href
                urls.append(full_url)
                if len(urls) >= max_reviews:
                    break
    return urls

def comment_on_review(page: Page, review_url: str, comment_text: str) -> bool:
    """Navigate to a specific review and attempt to post a comment."""
    try:
        page.goto(review_url, wait_until="networkidle")
        # Amazon's comment box usually appears when you click "Comment" button, then a textarea appears.
        # We'll try to find and click the "Comment" button first.
        comment_btn = page.query_selector('button:has-text("Comment")')
        if comment_btn:
            comment_btn.click()
            page.wait_for_timeout(1000)
        # Now look for a textarea
        comment_input = page.query_selector('textarea[name="commentText"], textarea#comment-text')
        if not comment_input:
            # Some pages use a contenteditable div
            comment_input = page.query_selector('div[contenteditable="true"]')
        if not comment_input:
            _post(f"No comment input found on {review_url}", "warning")
            return False
        comment_input.click()
        comment_input.type(comment_text)
        time.sleep(1)
        # Find and click the submit button
        submit_btn = page.query_selector('button[type="submit"], button:has-text("Post")')
        if submit_btn:
            submit_btn.click()
            page.wait_for_timeout(2000)
            return True
        else:
            _post(f"No submit button on {review_url}", "warning")
            return False
    except Exception as e:
        _post(f"Error commenting on {review_url}: {e}", "error")
        return False

def process_comments(config: dict, state: dict):
    """Main routine: log in, find product reviews, comment."""
    if not config.get("commenting", {}).get("enabled", False):
        return
    amazon_cfg = config["amazon"]
    commenting_cfg = config["commenting"]
    dry_run = commenting_cfg.get("dry_run", False)
    max_comments = int(commenting_cfg.get("max_comments_per_run", 2))
    products = commenting_cfg.get("products", [])
    comment_template = commenting_cfg.get("comment_template", "")
    link = commenting_cfg.get("link", "")
    already_commented = set(state.get("commented_review_ids", []))

    if not products or not comment_template:
        _post("Missing products or comment template", "warning")
        return

    with sync_playwright() as p:
        headless = amazon_cfg.get("headless", True)
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        if not amazon_login(page, amazon_cfg["email"], amazon_cfg["password"]):
            browser.close()
            return

        count = 0
        for product in products:
            asin = product.get("asin")
            product_url = f"https://www.amazon.com/dp/{asin}/ref=cm_cr_arp_d_product_top?ie=UTF8"
            # Go to product page to get review links
            review_urls = get_review_urls(page, product_url)
            for rev_url in review_urls:
                if count >= max_comments:
                    break
                # Use the review URL as a unique identifier (simplistic, but works)
                review_id = rev_url.split("/ref=")[0] if "/ref=" in rev_url else rev_url
                if review_id in already_commented:
                    continue
                comment = comment_template.replace("{link}", link)
                if dry_run:
                    _post(f"[DRY] Would comment on {review_id}: {comment[:60]}...", "info")
                    already_commented.add(review_id)
                    count += 1
                    continue
                success = comment_on_review(page, rev_url, comment)
                if success:
                    _post(f"Commented on review: {review_id}", "info")
                    already_commented.add(review_id)
                    count += 1
                    time.sleep(5)  # be polite
                else:
                    _post(f"Failed to comment on {review_id}", "error")
            if count >= max_comments:
                break

        state["commented_review_ids"] = list(already_commented)[-500:]
        browser.close()

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Amazon Review Comment Bot online")
    _post("Note: Amazon does not provide a comment API. This bot uses browser automation – success depends on anti‑bot measures.", "info")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "amazon_review_comment_state.json")
        state = load_state(state_file)

        process_comments(config, state)

        save_state(state_file, state)

        check_min = int(config.get("commenting", {}).get("check_interval_minutes", 360))
        _heartbeat()
        time.sleep(check_min * 60)

if __name__ == "__main__":
    main()
```
