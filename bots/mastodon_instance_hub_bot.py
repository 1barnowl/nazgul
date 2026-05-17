#!/usr/bin/env python3
"""
mastodon_instance_hub_bot.py — Mastodon Instance Hub Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Posts toots across multiple Mastodon servers (instances)
targeting specific communities, and auto‑replies to local
timelines to build presence. All actions reported to the
Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install Mastodon.py requests

Configuration
─────────────
Place `mastodon_hub_config.json` in the same directory:

{
  "accounts": [
    {
      "name": "MyMarketingBot",
      "instance": "https://mastodon.social",
      "access_token": "YOUR_ACCESS_TOKEN",
      "post_default": true,            // use this account for scheduled posts unless overridden
      "monitor": true                  // monitor local timeline and auto-reply
    },
    {
      "name": "MyTechBot",
      "instance": "https://fosstodon.org",
      "access_token": "ANOTHER_TOKEN",
      "post_default": false,
      "monitor": true
    }
  ],
  "scheduled_posts_file": "mastodon_scheduled_posts.json",
  "monitoring": {
    "enabled": true,
    "keywords": ["introduction", "marketing", "growth hacking", "SaaS"],
    "reply_template": "Thanks for sharing! I'm building a community around {topic}. Feel free to follow for more insights.",
    "max_replies_per_account_per_run": 2,
    "check_interval_minutes": 15
  },
  "state_file": "mastodon_hub_state.json",
  "heartbeat_interval": 30
}

Scheduled posts file (`mastodon_scheduled_posts.json`) format:
[
  {
    "account_index": 0,               // index into the accounts array (optional; default to first with post_default)
    "text": "Hello Mastodon! 🚀",
    "visibility": "public",
    "scheduled_at": "2025-01-25T09:00:00Z"
  }
]
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from mastodon import Mastodon

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "mastodon_instance_hub_bot"
BOT_NAME = "Mastodon Instance Hub"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "mastodon_hub_config.json"
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
        return {"replied_to_ids": {}, "scheduled_posted_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Mastodon client management ───────────────────────────────────
def get_clients(config: dict) -> List[Mastodon]:
    """Return list of authenticated Mastodon clients."""
    clients = []
    for acc in config.get("accounts", []):
        try:
            client = Mastodon(
                access_token=acc["access_token"],
                api_base_url=acc["instance"]
            )
            # Quick verify: fetch own account
            _ = client.me()
            clients.append(client)
        except Exception as e:
            _post(f"Could not log into {acc.get('name', acc['instance'])}: {e}", "error")
    return clients

# ── Scheduled publishing ─────────────────────────────────────────
def process_scheduled_posts(config: dict, state: dict):
    """Publish any due toots from the scheduled file."""
    file_path = config.get("scheduled_posts_file", "mastodon_scheduled_posts.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled posts file: {e}", "error")
        return

    if not scheduled:
        return

    accounts_cfg = config.get("accounts", [])
    clients = get_clients(config)  # re‑authenticate each cycle (could cache)
    if not clients:
        _post("No Mastodon accounts could be authenticated", "error")
        return

    now = datetime.now(timezone.utc)
    remaining = []
    posted_ids = set(state.get("scheduled_posted_ids", []))

    for item in scheduled:
        scheduled_at_str = item.get("scheduled_at")
        if not scheduled_at_str:
            remaining.append(item)
            continue
        try:
            scheduled_dt = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            remaining.append(item)
            continue

        # Check if already posted (using a generated ID based on content?)
        # We'll use index? Better to assign a unique ID from content hash.
        item_id = str(hash(json.dumps(item, sort_keys=True)))
        if item_id in posted_ids:
            # already processed, remove it from file
            continue

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            account_idx = item.get("account_index")
            # pick default account if not specified
            if account_idx is None:
                # use first account with post_default = true
                for i, acc in enumerate(accounts_cfg):
                    if acc.get("post_default", False):
                        account_idx = i
                        break
                if account_idx is None:
                    account_idx = 0  # fallback to first

            if account_idx >= len(clients):
                _post(f"Account index {account_idx} out of range", "error")
                remaining.append(item)
                continue

            client = clients[account_idx]
            text = item.get("text", "")
            visibility = item.get("visibility", "public")
            # Post toot
            try:
                status = client.toot(status=text, visibility=visibility)
                _post(f"Posted toot on {accounts_cfg[account_idx].get('name','?')}: {text[:50]}...", "info")
                posted_ids.add(item_id)
                # success: don't add to remaining
                time.sleep(1)
            except Exception as e:
                _post(f"Failed to post toot: {e}", "error")
                remaining.append(item)
        else:
            remaining.append(item)

    # Update state
    state["scheduled_posted_ids"] = list(posted_ids)
    # Write back remaining items
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Monitoring & auto‑reply ──────────────────────────────────────
def monitor_and_reply(config: dict, state: dict):
    """Check local timelines of monitored accounts and reply to matching toots."""
    if not config.get("monitoring", {}).get("enabled", False):
        return

    accounts_cfg = config.get("accounts", [])
    clients = get_clients(config)
    if not clients:
        return

    keywords = config["monitoring"]["keywords"]
    reply_template = config["monitoring"]["reply_template"]
    max_replies = int(config["monitoring"].get("max_replies_per_account_per_run", 2))
    replied_ids_map = state.setdefault("replied_to_ids", {})
    # replied_ids_map is a dict: account_name -> list of status IDs

    for idx, acc_cfg in enumerate(accounts_cfg):
        if not acc_cfg.get("monitor", True):
            continue
        client = clients[idx]
        acc_name = acc_cfg.get("name", acc_cfg["instance"])
        # Retrieve local timeline (public) – limit to 40 recent posts
        try:
            timeline = client.timeline_public(limit=40)
        except Exception as e:
            _post(f"Could not fetch local timeline for {acc_name}: {e}", "warning")
            continue

        # Get already replied IDs for this account
        replied_ids = set(replied_ids_map.get(acc_name, []))
        new_replied = 0

        for status in timeline:
            # Skip if already replied
            if status.id in replied_ids:
                continue
            # Skip own toots? (prevent self-reply) – check if status account id == client's own account
            try:
                me = client.me()
                if status.account.id == me.id:
                    continue
            except:
                pass

            content = status.content or ""
            # Strip HTML tags for matching (simple)
            import re
            text = re.sub(r'<[^>]+>', '', content).lower()
            # Check if any keyword appears
            if any(kw.lower() in text for kw in keywords):
                # Build personalised reply
                # Replace {topic} with first matched keyword
                matched_kw = next((kw for kw in keywords if kw.lower() in text), keywords[0])
                reply = reply_template.replace("{topic}", matched_kw)
                try:
                    client.status_reply(status, reply)
                    _post(f"Replied to toot {status.id} on {acc_name}: {reply[:50]}...", "info")
                    replied_ids.add(status.id)
                    new_replied += 1
                    time.sleep(1)
                    if new_replied >= max_replies:
                        break
                except Exception as e:
                    _post(f"Failed to reply to {status.id}: {e}", "error")

        # Update replied IDs for this account
        replied_ids_map[acc_name] = list(replied_ids)[-500:]  # keep last 500

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Mastodon Instance Hub Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "mastodon_hub_state.json")
        state = load_state(state_file)

        # Scheduled posts
        process_scheduled_posts(config, state)

        # Monitoring and auto‑reply
        monitor_and_reply(config, state)

        save_state(state_file, state)

        check_min = int(config.get("monitoring", {}).get("check_interval_minutes", 15)) * 60
        _heartbeat()
        time.sleep(check_min)

if __name__ == "__main__":
    main()
