#!/usr/bin/env python3
"""
reddit_multi_subreddit_bot.py — Reddit Multi‑Subreddit Automation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors keywords across target subreddits, posts genuinely helpful
comments and occasionally submits links, while rotating between
multiple accounts to avoid bans.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install praw requests

Configuration
─────────────
Place `reddit_multi_config.json` in the same directory:

{
  "accounts": [
    {
      "username": "bot_acc1",
      "password": "pass1",
      "client_id": "CLIENT_ID_1",
      "client_secret": "CLIENT_SECRET_1",
      "user_agent": "HelpfulBot/1.0 by /u/bot_acc1"
    },
    {
      "username": "bot_acc2",
      "password": "pass2",
      "client_id": "CLIENT_ID_2",
      "client_secret": "CLIENT_SECRET_2",
      "user_agent": "HelpfulBot/1.0 by /u/bot_acc2"
    }
  ],
  "monitoring": {
    "subreddits": ["python", "learnprogramming"],
    "keywords": ["automation", "web scraping", "data pipeline"],
    "comment_template": "I've built similar tools. You might find our open-source bot framework useful: https://github.com/example/bot-framework",
    "max_comments_per_account_per_run": 3,
    "min_comment_karma": 10,
    "post_age_limit_hours": 24
  },
  "submission": {
    "enabled": true,
    "subreddit": "python",
    "title_template": "Check out our new automation toolkit",
    "url": "https://github.com/example/bot-framework",
    "max_submissions_per_day": 1
  },
  "rate_limiting": {
    "cooldown_between_accounts_seconds": 5,
    "min_time_between_comments_seconds": 2
  },
  "state_file": "reddit_multi_state.json",
  "heartbeat_interval": 30,
  "poll_interval_seconds": 300
}
"""

import json
import os
import time
import random
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
import praw

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "reddit_multi_subreddit_bot"
BOT_NAME = "Reddit Multi‑Subreddit Automation"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "reddit_multi_config.json"
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
            "commented_ids": {},
            "last_submission_date": "",
            "account_usage": {}
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Reddit client management ─────────────────────────────────────
def get_reddit_clients(config: dict) -> List[praw.Reddit]:
    """Create and return a list of authenticated Reddit instances."""
    clients = []
    for acc_cfg in config.get("accounts", []):
        try:
            reddit = praw.Reddit(
                client_id=acc_cfg["client_id"],
                client_secret=acc_cfg["client_secret"],
                password=acc_cfg["password"],
                username=acc_cfg["username"],
                user_agent=acc_cfg["user_agent"]
            )
            # Verify by fetching username
            me = reddit.user.me()
            if me is None:
                _post(f"Could not authenticate account {acc_cfg['username']}", "error")
                continue
            clients.append(reddit)
        except Exception as e:
            _post(f"Error authenticating {acc_cfg.get('username', '?')}: {e}", "error")
    return clients

# ── Monitoring & commenting ──────────────────────────────────────
def monitor_and_comment(config: dict, state: dict):
    """Scan recent posts in target subreddits for keywords, comment if match."""
    monitoring = config.get("monitoring", {})
    if not monitoring:
        return

    subreddits = monitoring.get("subreddits", [])
    keywords = [kw.lower() for kw in monitoring.get("keywords", [])]
    comment_template = monitoring.get("comment_template", "")
    max_comments_per_account = int(monitoring.get("max_comments_per_account_per_run", 3))
    min_karma = int(monitoring.get("min_comment_karma", 10))
    age_limit_hours = float(monitoring.get("post_age_limit_hours", 24))

    if not subreddits or not keywords or not comment_template:
        _post("Monitoring configuration incomplete", "warning")
        return

    # Get authenticated clients (accounts)
    clients = get_reddit_clients(config)
    if not clients:
        _post("No Reddit accounts available", "error")
        return

    # Track already commented IDs (state)
    commented_ids = state.setdefault("commented_ids", {})

    # Cooldown settings
    cooldown_between_accounts = float(config.get("rate_limiting", {}).get("cooldown_between_accounts_seconds", 5))
    min_time_between_comments = float(config.get("rate_limiting", {}).get("min_time_between_comments_seconds", 2))

    # We'll process each account sequentially, each doing a few comments
    total_comments = 0
    for client in clients:
        # How many comments this account can still make in this run?
        already_done = state.setdefault("account_usage", {}).get(str(client.user.me().name), {}).get("today_comments", 0)
        remaining = max(0, max_comments_per_account - already_done)
        if remaining <= 0:
            continue

        # For each subreddit
        for sub_name in subreddits:
            try:
                subreddit = client.subreddit(sub_name)
                # Fetch new posts (last 50)
                for submission in subreddit.new(limit=50):
                    if total_comments >= len(clients) * max_comments_per_account:
                        break
                    if remaining <= 0:
                        break

                    # Check if we already commented on this submission (by any account? we use submission ID)
                    sub_id = submission.id
                    if sub_id in commented_ids:
                        continue

                    # Age check
                    created_time = datetime.utcfromtimestamp(submission.created_utc).replace(tzinfo=timezone.utc)
                    age = datetime.now(timezone.utc) - created_time
                    if age.total_seconds() > age_limit_hours * 3600:
                        continue

                    # Keyword detection in title + selftext
                    text = (submission.title + " " + submission.selftext).lower()
                    if any(kw in text for kw in keywords):
                        # Post a comment
                        try:
                            submission.reply(comment_template)
                            _post(f"Commented on {sub_id} in r/{sub_name}", "info", {"submission_id": sub_id})
                            commented_ids[sub_id] = True
                            # Update usage
                            already_done += 1
                            remaining -= 1
                            total_comments += 1
                            state.setdefault("account_usage", {}).setdefault(str(client.user.me().name), {})["today_comments"] = already_done
                            time.sleep(min_time_between_comments)
                        except Exception as e:
                            _post(f"Failed to comment on {sub_id}: {e}", "error")
            except Exception as e:
                _post(f"Error accessing r/{sub_name}: {e}", "warning")
        # Cooldown between accounts
        time.sleep(cooldown_between_accounts)

# ── Submission (occasional link posting) ────────────────────────
def submit_link(config: dict, state: dict):
    """Post a link to a subreddit if allowed and not already done today."""
    submission_cfg = config.get("submission", {})
    if not submission_cfg.get("enabled", False):
        return

    subreddit = submission_cfg.get("subreddit")
    title = submission_cfg.get("title_template", "")
    url = submission_cfg.get("url", "")
    max_per_day = int(submission_cfg.get("max_submissions_per_day", 1))

    if not all([subreddit, title, url]):
        _post("Submission configuration incomplete", "warning")
        return

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    last_date = state.get("last_submission_date", "")
    if last_date == today:
        return  # already submitted today

    # Use the first account that has enough karma? We'll just use the first account
    clients = get_reddit_clients(config)
    if not clients:
        return
    client = clients[0]

    try:
        sub = client.subreddit(subreddit)
        sub.submit(title=title, url=url)
        _post(f"Submitted link to r/{subreddit}: {title}", "info")
        state["last_submission_date"] = today
        # Reset account usage? Not necessary
    except Exception as e:
        _post(f"Failed to submit link: {e}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Reddit Multi‑Subreddit Automation Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "reddit_multi_state.json")
        state = load_state(state_file)

        # Monitoring and commenting
        monitor_and_comment(config, state)

        # Link submission (if enabled and due)
        submit_link(config, state)

        save_state(state_file, state)

        poll_sec = int(config.get("poll_interval_seconds", 300))
        _heartbeat()
        time.sleep(poll_sec)

if __name__ == "__main__":
    main()
