#!/usr/bin/env python3
"""
medium_syndication_bot.py — Medium Syndication Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Publishes long‑form articles via the Medium API and
engages with related content by clapping and commenting
using browser automation (Playwright).  Cross‑pollinates
audiences between publications.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests playwright
    playwright install chromium

Configuration
─────────────
Place `medium_syndication_config.json` in the same directory:

{
  "medium": {
    "api_token": "YOUR_MEDIUM_INTEGRATION_TOKEN",   // from https://medium.com/me/settings
    "author_id": "YOUR_AUTHOR_ID",                  // get via GET https://api.medium.com/v1/me using the token
    "username": "your_email@example.com",           // for Playwright login (clapping/comments)
    "password": "your_password",
    "headless": true
  },
  "publishing": {
    "enabled": true,
    "articles_file": "medium_scheduled_articles.json"
  },
  "engagement": {
    "enabled": true,
    "publication_slugs": ["marketing", "technology"],  // publications or tags to browse
    "max_claps": 10,                                    // claps per article (1-50)
    "comment_template": "Great read! I've written a similar article: https://your-site.com",
    "keywords": ["automation", "bot"],                 // optional; if present, only articles containing these will be engaged
    "max_articles_to_engage": 3,
    "reply_to_comments_on_own_articles": true,
    "own_article_reply_template": "Thanks for your feedback!",
    "check_interval_minutes": 60
  },
  "state_file": "medium_syndication_state.json",
  "heartbeat_interval": 30
}

Scheduled articles file (`medium_scheduled_articles.json`):
[
  {
    "title": "How to Automate Your Marketing with Python",
    "content_html": "<h1>Introduction</h1><p>Full article HTML...</p>",
    "tags": ["python", "automation"],
    "publication_id": null,                         // optional
    "canonical_url": "https://your-blog.com/original",
    "publish_status": "public",                     // "public" or "draft"
    "scheduled_at": "2025-02-15T10:00:00Z"
  }
]
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
from playwright.sync_api import sync_playwright, Page

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "medium_syndication_bot"
BOT_NAME = "Medium Syndication"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "medium_syndication_config.json"
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
            "published_article_ids": [],      # IDs of articles we published (by hashed content)
            "engaged_article_urls": [],
            "replied_comment_ids": []
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Medium API (publishing) ──────────────────────────────────────
MEDIUM_API = "https://api.medium.com/v1"

def _medium_headers(api_token: str) -> dict:
    return {
        "Authorization": f"Bearer {api_token}",
        "Content-Type": "application/json"
    }

def publish_medium_article(api_token: str, author_id: str, article: dict) -> Optional[str]:
    """
    Publish an article. Returns the article's Medium URL or None.
    """
    url = f"{MEDIUM_API}/users/{author_id}/posts"
    data = {
        "title": article.get("title", ""),
        "contentFormat": "html",
        "content": article.get("content_html", ""),
        "tags": article.get("tags", []),
        "publishStatus": article.get("publish_status", "public"),
        "canonicalUrl": article.get("canonical_url"),
        "publicationId": article.get("publication_id")
    }
    # Remove None keys
    data = {k: v for k, v in data.items() if v is not None}
    try:
        resp = requests.post(url, json=data, headers=_medium_headers(api_token), timeout=15)
        if resp.status_code in (200, 201):
            result = resp.json()
            # Response contains "data" with "url", "id", etc.
            return result.get("data", {}).get("url")
        else:
            _post(f"Medium API error {resp.status_code}: {resp.text[:300]}", "error")
            return None
    except Exception as e:
        _post(f"Medium publish error: {e}", "error")
        return None

def process_scheduled_articles(config: dict, state: dict):
    """Publish articles that are due."""
    if not config.get("publishing", {}).get("enabled", False):
        return
    file_path = config["publishing"].get("articles_file", "medium_scheduled_articles.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            articles = json.load(f)
    except Exception as e:
        _post(f"Error reading articles file: {e}", "error")
        return

    api_token = config["medium"]["api_token"]
    author_id = config["medium"]["author_id"]
    if not api_token or not author_id:
        _post("Medium API token or author ID missing", "error")
        return

    now = datetime.now(timezone.utc)
    remaining = []
    published_ids = set(state.get("published_article_ids", []))

    for article in articles:
        scheduled_at_str = article.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(article)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(article)
            continue

        # Unique hash for this article (title + content)
        content_hash = str(hash(article.get("title") + article.get("content_html", "")))
        if content_hash in published_ids:
            continue  # already published

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            result_url = publish_medium_article(api_token, author_id, article)
            if result_url:
                _post(f"Published article: {article['title'][:60]} → {result_url}", "info")
                published_ids.add(content_hash)
                # success, remove from queue
                continue
            else:
                _post("Failed to publish article, keeping for retry", "error")
        remaining.append(article)

    state["published_article_ids"] = list(published_ids)[-500:]
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Engagement via Playwright ────────────────────────────────────
def medium_login(page: Page, username: str, password: str) -> bool:
    """Log in to Medium using Playwright."""
    try:
        page.goto("https://medium.com/m/signin", wait_until="networkidle")
        # Use email sign-in
        page.click('button:has-text("Sign in with email")')
        page.wait_for_selector('input[name="email"]', timeout=10000)
        page.fill('input[name="email"]', username)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        # Wait for home page
        if "medium.com" in page.url and "signin" not in page.url:
            _post("Logged into Medium successfully", "info")
            return True
        else:
            _post("Medium login might have failed; check credentials.", "warning")
            return False
    except Exception as e:
        _post(f"Medium login error: {e}", "error")
        return False

def search_articles_by_topic(page: Page, topic: str, keywords: List[str] = None, max_articles: int = 5) -> List[str]:
    """Search Medium for recent articles on a topic and return their URLs."""
    page.goto(f"https://medium.com/search?q={topic}", wait_until="networkidle")
    # Scroll to load more
    for _ in range(3):
        page.evaluate("window.scrollBy(0, 1500)")
        time.sleep(1)
    # Grab article links (they typically have href="/@username/article-title-xxxx")
    article_links = page.query_selector_all('a[data-action="click-post"]')
    urls = []
    for link in article_links:
        href = link.get_attribute("href")
        if href and href.startswith("/"):
            full_url = "https://medium.com" + href
            # remove query params? keep
            urls.append(full_url)
        if len(urls) >= max_articles:
            break
    # If no articles, try different selector
    if not urls:
        # fallback: all links with href containing '/p/' or '/@'
        links = page.query_selector_all('a[href*="/p/"]')
        for link in links:
            href = link.get_attribute("href")
            if href and not href.startswith("http"):
                href = "https://medium.com" + href
            if href:
                urls.append(href)
            if len(urls) >= max_articles:
                break
    return urls

def clap_article(page: Page, article_url: str, count: int = 10) -> bool:
    """Navigate to an article and clap up to `count` times (max 50)."""
    try:
        page.goto(article_url, wait_until="networkidle")
        # Wait for the clap button to be visible
        clap_btn = page.wait_for_selector('button[data-action="show-recommendations"]', timeout=10000)
        if not clap_btn:
            return False
        # Click repeatedly to add claps
        for _ in range(min(count, 50)):
            clap_btn.click()
            time.sleep(0.2)
        return True
    except Exception as e:
        _post(f"Clap error for {article_url}: {e}", "error")
        return False

def comment_on_article(page: Page, article_url: str, comment_text: str) -> bool:
    """Navigate to an article and post a comment."""
    try:
        page.goto(article_url, wait_until="networkidle")
        # Scroll to the comment section
        page.evaluate("window.scrollBy(0, 2000)")
        page.wait_for_selector('div.notes-list', timeout=5000)  # comment area
        # Open the response form
        write_btn = page.query_selector('button[data-action="respond-to-post"]')
        if not write_btn:
            # maybe already in the page
            pass
        else:
            write_btn.click()
        # Wait for editor
        editor = page.wait_for_selector('div[contenteditable="true"]', timeout=5000)
        if not editor:
            return False
        editor.click()
        editor.type(comment_text)
        time.sleep(1)
        # Submit comment (usually a button with text "Publish" or "Respond")
        submit_btn = page.query_selector('button[type="submit"]')
        if submit_btn:
            submit_btn.click()
            time.sleep(2)
            return True
        return False
    except Exception as e:
        _post(f"Comment error for {article_url}: {e}", "error")
        return False

def process_engagement(config: dict, state: dict):
    """Use Playwright to browse topics and clap/comment on articles."""
    if not config.get("engagement", {}).get("enabled", False):
        return
    medium_cfg = config["medium"]
    username = medium_cfg.get("username")
    password = medium_cfg.get("password")
    if not username or not password:
        _post("Medium username/password not configured for engagement", "error")
        return

    engagement_cfg = config["engagement"]
    topics = engagement_cfg.get("publication_slugs", [])
    keywords = engagement_cfg.get("keywords")
    max_articles = int(engagement_cfg.get("max_articles_to_engage", 3))
    comment_template = engagement_cfg.get("comment_template", "")
    max_claps = int(engagement_cfg.get("max_claps", 10))
    engage_own = engagement_cfg.get("reply_to_comments_on_own_articles", False)

    # Already engaged articles
    engaged_urls = set(state.get("engaged_article_urls", []))

    with sync_playwright() as p:
        headless = medium_cfg.get("headless", True)
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()

        if not medium_login(page, username, password):
            browser.close()
            return

        # For each topic, find articles and engage
        new_articles = []
        for topic in topics:
            urls = search_articles_by_topic(page, topic, keywords, max_articles=max_articles)
            new_articles.extend(urls)

        # Deduplicate
        new_articles = list(dict.fromkeys(new_articles))  # preserve order, unique

        engaged_count = 0
        for article_url in new_articles:
            if engaged_count >= max_articles:
                break
            if article_url in engaged_urls:
                continue

            # Clap
            clap_article(page, article_url, max_claps)
            # Comment (if template non-empty)
            if comment_template:
                comment_on_article(page, article_url, comment_template)
                time.sleep(1)

            _post(f"Engaged with article: {article_url}", "info")
            engaged_urls.add(article_url)
            engaged_count += 1
            time.sleep(2)  # polite delay

        state["engaged_article_urls"] = list(engaged_urls)[-500:]
        browser.close()

    # Reply to comments on own articles (optional)
    # This would require fetching our articles via API (list of published URLs) and then for each, checking comments, replying.
    # Since Medium doesn't provide a comments API, we'd need to use Playwright as well. We'll skip for now due to complexity,
    # but we'll log a note.
    if engage_own:
        _post("Replying to comments on own articles is not currently implemented via Playwright – can be added.", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Medium Syndication Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "medium_syndication_state.json")
        state = load_state(state_file)

        # Publishing via API
        process_scheduled_articles(config, state)

        # Engagement via Playwright (separate interval)
        process_engagement(config, state)

        save_state(state_file, state)

        check_minutes = int(config.get("engagement", {}).get("check_interval_minutes", 60))
        _heartbeat()
        time.sleep(check_minutes * 60)

if __name__ == "__main__":
    main()
