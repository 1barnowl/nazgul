#!/usr/bin/env python3
"""
quora_answer_space_bot.py — Quora Answer & Space Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Finds questions with high search volume (measured by
follower count), generates in‑depth answers with embedded
links using an optional LLM, posts them, and optionally
submits content to Quora Spaces.  Uses Playwright to
automate the Quora web interface because no public API
exists.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install playwright requests
    playwright install chromium

Configuration
─────────────
Place `quora_bot_config.json` in the same directory:

{
  "quora": {
    "email": "your_email@example.com",
    "password": "your_password",
    "headless": true
  },
  "answering": {
    "enabled": true,
    "keywords": ["python automation", "web scraping"],
    "min_followers": 5,
    "answer_template": "That's a great question! I've written a detailed guide that covers this topic: {link}\n\nIn addition, here's a quick summary: ...",
    "link": "https://example.com/your-guide",
    "llm": null                            // optional, set to { "provider": "openai", "api_key": "sk-...", "model": "gpt-4o-mini" } for generated answers
  },
  "spaces": {
    "enabled": false,
    "space_urls": ["https://www.quora.com/space/YourSpace"],
    "post_template": "I just wrote an answer on {topic}: {link}",
    "max_posts_per_space": 1
  },
  "state_file": "quora_bot_state.json",
  "heartbeat_interval": 30,
  "poll_interval_hours": 24
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
BOT_ID = "quora_answer_space_bot"
BOT_NAME = "Quora Answer & Space"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "quora_bot_config.json"
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
        return {"answered_questions": [], "space_posts": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── LLM call (optional) ──────────────────────────────────────────
def generate_answer_with_llm(question_text: str, llm_config: dict) -> Optional[str]:
    """Generate a helpful answer using an LLM."""
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
            {"role": "system", "content": "You are a helpful assistant who writes in-depth, informative answers on Quora. Include a link to the user's resource naturally."},
            {"role": "user", "content": f"Question: {question_text}\n\nWrite a detailed answer that adds value and includes a reference to our guide at {llm_config.get('link', '')}"}
        ],
        "temperature": 0.7,
        "max_tokens": 800
    }
    try:
        resp = requests.post(endpoint, headers=headers, json=data, timeout=20)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            _post(f"LLM error: {resp.status_code}", "warning")
            return None
    except Exception as e:
        _post(f"LLM request failed: {e}", "error")
        return None

# ── Playwright automation ────────────────────────────────────────
def quora_login(page: Page, email: str, password: str) -> bool:
    """Log into Quora using email/password."""
    try:
        page.goto("https://www.quora.com/", wait_until="networkidle")
        # If already logged in, user avatar visible
        if page.query_selector('img[alt="Profile Photo"]'):
            return True
        # Click login button
        page.click('text=Log In')
        page.wait_for_selector('input[name="email"]', timeout=10000)
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        # Wait for the homepage to load
        time.sleep(3)
        if page.query_selector('img[alt="Profile Photo"]'):
            return True
        else:
            _post("Quora login may have failed; continuing anyway", "warning")
            return True
    except Exception as e:
        _post(f"Quora login error: {e}", "error")
        return False

def search_questions(page: Page, keyword: str, min_followers: int) -> List[dict]:
    """
    Search for a keyword, navigate to the question list, and scrape questions with follower count.
    Returns list of {text, url, followers}.
    """
    questions = []
    try:
        page.goto(f"https://www.quora.com/search?q={keyword}&type=question", wait_until="networkidle")
        # Scroll to load more
        for _ in range(3):
            page.evaluate("window.scrollBy(0, 1000)")
            time.sleep(1)
        # Parse question cards
        card_selector = 'div[class*="QuestionCard"]'
        cards = page.query_selector_all(card_selector)
        for card in cards:
            # Title link
            link_el = card.query_selector('a[class*="question_link"]')
            if not link_el:
                continue
            url = link_el.get_attribute("href")
            text = link_el.inner_text().strip()
            # Follower count
            follower_el = card.query_selector('span[class*="FollowerCount"]')
            followers = 0
            if follower_el:
                follower_text = follower_el.inner_text().split()[0]  # e.g., "3.1k"
                if 'k' in follower_text:
                    followers = int(float(follower_text.replace('k', '')) * 1000)
                else:
                    followers = int(follower_text) if follower_text.isdigit() else 0
            if followers >= min_followers:
                questions.append({"text": text, "url": "https://www.quora.com" + url, "followers": followers})
    except Exception as e:
        _post(f"Search error for '{keyword}': {e}", "error")
    return questions

def post_answer(page: Page, question_url: str, answer_text: str) -> bool:
    """Navigate to a question, open answer form, and submit."""
    try:
        page.goto(question_url, wait_until="networkidle")
        # Look for the answer text area
        answer_area = page.query_selector('textarea#root_answer')
        if not answer_area:
            # Maybe click "Answer" button first
            answer_btn = page.query_selector('text=Answer')
            if answer_btn:
                answer_btn.click()
                page.wait_for_selector('textarea#root_answer', timeout=5000)
                answer_area = page.query_selector('textarea#root_answer')
        if not answer_area:
            _post(f"Could not find answer text area on {question_url}", "error")
            return False
        answer_area.fill(answer_text)
        # Submit
        submit_btn = page.query_selector('button[type="submit"]')
        if submit_btn:
            submit_btn.click()
            page.wait_for_timeout(3000)
            return True
        else:
            return False
    except Exception as e:
        _post(f"Failed to answer {question_url}: {e}", "error")
        return False

def post_to_space(page: Page, space_url: str, content: str) -> bool:
    """Create a post inside a Quora Space."""
    try:
        page.goto(space_url, wait_until="networkidle")
        # Click "Create post"
        create_btn = page.query_selector('text=Create Post')
        if create_btn:
            create_btn.click()
            page.wait_for_selector('div.public-DraftEditor-content', timeout=5000)
            # Fill the editor
            editor = page.query_selector('div.public-DraftEditor-content')
            if editor:
                editor.fill(content)  # actually it's a contenteditable div, fill might not work; use type instead
                page.keyboard.type(content)
                time.sleep(1)
                # Publish
                publish_btn = page.query_selector('button:has-text("Publish")')
                if publish_btn:
                    publish_btn.click()
                    page.wait_for_timeout(3000)
                    return True
        return False
    except Exception as e:
        _post(f"Space posting error: {e}", "error")
        return False

# ── Main logic ───────────────────────────────────────────────────
def main():
    _post("Quora Answer & Space Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        email = config["quora"]["email"]
        password = config["quora"]["password"]
        headless = config["quora"].get("headless", True)

        answering = config.get("answering", {})
        spaces = config.get("spaces", {})
        state_file = config.get("state_file", "quora_bot_state.json")
        state = load_state(state_file)
        answered_ids = set(state.get("answered_questions", []))

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            if not quora_login(page, email, password):
                browser.close()
                time.sleep(300)
                continue

            # Answering
            if answering.get("enabled", False):
                keywords = answering.get("keywords", [])
                min_followers = int(answering.get("min_followers", 5))
                template = answering.get("answer_template", "")
                link = answering.get("link", "")
                llm_cfg = answering.get("llm")

                for kw in keywords:
                    questions = search_questions(page, kw, min_followers)
                    for q in questions:
                        if q["url"] in answered_ids:
                            continue
                        # Generate answer
                        if llm_cfg:
                            answer = generate_answer_with_llm(q["text"], llm_cfg)
                            if not answer:
                                answer = template.replace("{link}", link)
                        else:
                            answer = template.replace("{link}", link)
                        success = post_answer(page, q["url"], answer)
                        if success:
                            _post(f"Answered: {q['text'][:80]}", "info", {"question_url": q["url"]})
                            answered_ids.add(q["url"])
                            state["answered_questions"] = list(answered_ids)[-500:]
                            time.sleep(5)  # be polite
                            break  # one answer per keyword per run to avoid spam
                    if any(q["url"] in answered_ids for q in questions):
                        continue  # already answered one for this keyword
            # Spaces posting (simplified)
            if spaces.get("enabled", False):
                space_urls = spaces.get("space_urls", [])
                post_template = spaces.get("post_template", "")
                if post_template and link:
                    content = post_template.replace("{link}", link)
                    for space_url in space_urls:
                        if space_url in state.get("space_posts", []):
                            continue
                        if post_to_space(page, space_url, content):
                            _post(f"Posted in Space: {space_url}", "info")
                            state.setdefault("space_posts", []).append(space_url)
                            time.sleep(3)

            browser.close()

        save_state(state_file, state)
        poll_hours = float(config.get("poll_interval_hours", 24))
        _heartbeat()
        time.sleep(poll_hours * 3600)

if __name__ == "__main__":
    main()
