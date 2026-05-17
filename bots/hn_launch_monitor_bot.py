#!/usr/bin/env python3
"""
hn_launch_monitor_bot.py — Hacker News Launch & Monitor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Handles “Show HN” posts, monitors comments on your post
via the Firebase API, and auto‑replies to keep your launch
visible.  Can optionally upvote your own submission (if HN
allows it).  Uses a simple HTTP session – no browser automation.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `hn_launch_config.json` in the same directory:

{
  "hn": {
    "username": "your_hn_username",
    "password": "your_hn_password",
    "login_url": "https://news.ycombinator.com/login",
    "submit_url": "https://news.ycombinator.com/submit"
  },
  "launch": {
    "title": "Show HN: My Awesome Project",
    "url": "https://myproject.com",
    "show_hn": true,
    "scheduled_at": "2025-01-30T12:00:00Z"
  },
  "auto_reply": {
    "enabled": true,
    "reply_template": "Thanks for your feedback! We'll take it into account. More details: https://myproject.com/about",
    "llm": null,
    "poll_interval_seconds": 60,
    "max_replies_per_run": 5
  },
  "upvote": {
    "enabled": false,
    "accounts": []                    // array of {username, password} to upvote
  },
  "state_file": "hn_launch_state.json",
  "heartbeat_interval": 30
}
"""

import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "hn_launch_monitor_bot"
BOT_NAME = "Hacker News Launch & Monitor"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "hn_launch_config.json"
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
        return {"submitted_item_id": None, "replied_comment_ids": [], "last_upvote_time": None}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Hacker News HTTP helpers ─────────────────────────────────────
class HNSession:
    def __init__(self, username: str, password: str):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Mozilla/5.0 (compatible; HNBot/1.0)"})
        self.username = username
        self.logged_in = False
        if not self.login(username, password):
            _post("HN login failed. Some actions may not work.", "warning")
        else:
            _post("Logged into Hacker News successfully", "info")

    def login(self, username: str, password: str) -> bool:
        login_url = "https://news.ycombinator.com/login"
        # First get the login page to extract the CSRF token (hn uses "goto" and "acct" fields)
        try:
            resp = self.session.get(login_url)
            if resp.status_code != 200:
                return False
            # Extract fnid? Actually HN login requires a POST with 'acct', 'pw', and 'goto' hidden fields.
            # We'll parse the form: name="acct" and name="pw" and hidden 'goto'.
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            form = soup.find('form', action='login')
            if not form:
                return False
            acct_input = form.find('input', {'name': 'acct'})
            if not acct_input:
                return False
            # Fill in the fields
            data = {}
            for inp in form.find_all('input'):
                if inp.get('type') == 'submit':
                    continue
                name = inp.get('name')
                value = inp.get('value', '')
                if name == 'acct':
                    value = username
                elif name == 'pw':
                    value = password
                data[name] = value
            action = form.get('action', 'login')
            if action.startswith('/'):
                action = "https://news.ycombinator.com" + action
            else:
                action = "https://news.ycombinator.com/" + action
            resp = self.session.post(action, data=data, allow_redirects=True)
            # Check if login succeeded by looking for logout link
            if 'logout' in resp.text.lower():
                self.logged_in = True
                return True
            return False
        except Exception as e:
            _post(f"Login error: {e}", "error")
            return False

    def submit_story(self, title: str, url: str, show_hn: bool = True) -> Optional[int]:
        """Submit a new story and return the item ID if successful."""
        if not self.logged_in:
            _post("Not logged in; cannot submit story.", "error")
            return None
        submit_url = "https://news.ycombinator.com/submit"
        try:
            # Need the submission page to get the fnid token
            resp = self.session.get(submit_url)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            form = soup.find('form', action=re.compile(r'.*r'))
            if not form:
                _post("Could not find submission form", "error")
                return None
            data = {}
            for inp in form.find_all('input'):
                name = inp.get('name')
                value = inp.get('value', '')
                if name in ('title', 'url'):
                    continue
                data[name] = value
            data['title'] = title
            data['url'] = url
            if show_hn:
                # Show HN is just a prefix in the title; no special field.
                # We already have "Show HN:" in title per config; but HN detects it automatically.
                pass
            action = form.get('action')
            if action.startswith('/'):
                action = "https://news.ycombinator.com" + action
            else:
                action = "https://news.ycombinator.com/" + action
            resp = self.session.post(action, data=data, allow_redirects=True)
            if resp.status_code != 200:
                return None
            # The response URL should be the new item page, e.g., /item?id=...
            redirect_url = resp.url
            if 'id=' in redirect_url:
                item_id = redirect_url.split('id=')[-1]
                return int(item_id)
            else:
                # Could be an error; try to extract
                return None
        except Exception as e:
            _post(f"Submit error: {e}", "error")
            return None

    def add_comment(self, parent_id: int, text: str) -> bool:
        """Post a comment to an existing item. parent_id is the story or comment ID."""
        if not self.logged_in:
            return False
        reply_url = f"https://news.ycombinator.com/item?id={parent_id}"
        try:
            # First get the page to find the comment form
            resp = self.session.get(reply_url)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            # Comment form is inside a <form> with method="post" and action="comment"
            form = soup.find('form', action='comment')
            if not form:
                _post(f"No comment form found for item {parent_id}", "error")
                return False
            data = {}
            for inp in form.find_all('input'):
                name = inp.get('name')
                value = inp.get('value', '')
                if name == 'parent':
                    value = str(parent_id)
                if name:
                    data[name] = value
            # The textarea is named 'text'
            data['text'] = text
            action = form.get('action')
            if action.startswith('/'):
                action = "https://news.ycombinator.com" + action
            else:
                action = "https://news.ycombinator.com/" + action
            resp = self.session.post(action, data=data, allow_redirects=True)
            return resp.status_code == 200
        except Exception as e:
            _post(f"Comment error: {e}", "error")
            return False

    def upvote(self, item_id: int) -> bool:
        """Upvote a story. Requires auth token in the URL."""
        if not self.logged_in:
            return False
        # The upvote URL contains an auth token which we need to extract from the page.
        # First fetch the item page to get the auth link for the upvote.
        try:
            resp = self.session.get(f"https://news.ycombinator.com/item?id={item_id}")
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp.text, 'html.parser')
            # Find the upvote link: <a id="up_<item_id>" href="vote?id=<item_id>&how=up&auth=<token>&goto=...">
            up_link = soup.find('a', id=f"up_{item_id}")
            if not up_link:
                _post(f"No upvote link for item {item_id} (maybe not logged in or already voted).", "info")
                return False
            href = up_link.get('href')
            if not href:
                return False
            url = "https://news.ycombinator.com/" + href
            resp = self.session.get(url, allow_redirects=True)
            return resp.status_code == 200
        except Exception as e:
            _post(f"Upvote error: {e}", "error")
            return False

# ── Firebase API comment monitor ─────────────────────────────────
def fetch_item(item_id: int) -> dict:
    """Retrieve an item via the Firebase API."""
    try:
        resp = requests.get(f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json", timeout=10)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return {}

def get_new_comments(story_id: int, processed_ids: set) -> List[dict]:
    """Recursively extract all comment IDs and their text, skipping already processed ones."""
    story = fetch_item(story_id)
    if not story:
        return []
    comments = []
    # Process kids (comment IDs)
    stack = story.get('kids', [])[:]  # copy
    visited = set(processed_ids)
    while stack:
        comment_id = stack.pop()
        if comment_id in visited:
            continue
        visited.add(comment_id)
        comment = fetch_item(comment_id)
        if not comment or comment.get('dead') or comment.get('deleted'):
            continue
        text = comment.get('text', '')
        comments.append({"id": comment_id, "text": text})
        # Add children to stack
        kids = comment.get('kids', [])
        stack.extend(kids)
    return comments

# ── LLM (optional) ───────────────────────────────────────────────
def generate_reply(comment_text: str, llm_config: dict) -> Optional[str]:
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
    prompt = f"You are a friendly startup founder on Hacker News. A user commented:\n\n{comment_text}\n\nWrite a short, humble, and useful reply. Include a link to our product if relevant: {llm_config.get('link', 'https://myproject.com')}"
    data = {
        "model": llm_config.get("model", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        "max_tokens": 300
    }
    try:
        resp = requests.post(endpoint, headers=headers, json=data, timeout=15)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            _post(f"LLM error: {resp.status_code}", "warning")
            return None
    except Exception as e:
        _post(f"LLM request failed: {e}", "error")
        return None

# ── Main logic ───────────────────────────────────────────────────
def main():
    _post("Hacker News Launch & Monitor Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        hn_cfg = config.get("hn", {})
        username = hn_cfg.get("username")
        password = hn_cfg.get("password")
        if not username or not password:
            _post("HN credentials missing", "error")
            time.sleep(300)
            continue

        launch_cfg = config.get("launch", {})
        auto_reply_cfg = config.get("auto_reply", {})
        upvote_cfg = config.get("upvote", {})
        state_file = config.get("state_file", "hn_launch_state.json")
        state = load_state(state_file)

        # Prepare session
        session = HNSession(username, password)

        # ── Scheduled Launch ──────────────────────────────────────
        scheduled_at_str = launch_cfg.get("scheduled_at")
        now = datetime.now(timezone.utc)
        if scheduled_at_str and not state.get("submitted_item_id"):
            try:
                scheduled_dt = datetime.fromisoformat(scheduled_at_str)
            except ValueError:
                _post("Invalid scheduled_at format", "error")
            else:
                if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
                    title = launch_cfg.get("title", "")
                    url = launch_cfg.get("url", "")
                    show_hn = launch_cfg.get("show_hn", True)
                    item_id = session.submit_story(title, url, show_hn)
                    if item_id:
                        _post(f"Show HN submitted! Item ID: {item_id}", "warning", {"item_id": item_id})
                        state["submitted_item_id"] = item_id
                        # Upvote? If enabled and own account, we can upvote ourselves (HN might block, but try)
                        if upvote_cfg.get("enabled", False):
                            # Upvote with the same account (or other accounts)
                            if session.upvote(item_id):
                                _post(f"Upvoted own submission {item_id}", "info")
                            # Additional accounts? Not implemented here
                    else:
                        _post("Failed to submit Show HN post", "error")
        # ── Monitor comments ──────────────────────────────────────
        item_id = state.get("submitted_item_id")
        if item_id and auto_reply_cfg.get("enabled", False):
            replied_ids = set(state.get("replied_comment_ids", []))
            new_comments = get_new_comments(item_id, replied_ids)
            max_replies = int(auto_reply_cfg.get("max_replies_per_run", 5))
            count = 0
            for comment in new_comments:
                if count >= max_replies:
                    break
                reply_text = auto_reply_cfg.get("reply_template", "")
                # Use LLM if configured
                llm_cfg = auto_reply_cfg.get("llm")
                if llm_cfg:
                    generated = generate_reply(comment["text"], llm_cfg)
                    if generated:
                        reply_text = generated
                if session.add_comment(comment["id"], reply_text):
                    _post(f"Replied to comment {comment['id']}", "info", {"comment_id": comment["id"]})
                    replied_ids.add(comment["id"])
                    state["replied_comment_ids"] = list(replied_ids)[-500:]
                    count += 1
                    time.sleep(2)  # polite delay
                else:
                    _post(f"Failed to reply to {comment['id']}", "error")
        # Save state
        save_state(state_file, state)

        poll_sec = int(auto_reply_cfg.get("poll_interval_seconds", 60))
        _heartbeat()
        time.sleep(poll_sec)

if __name__ == "__main__":
    main()
