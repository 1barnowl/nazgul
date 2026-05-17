#!/usr/bin/env python3
"""
pinterest_pin_bot.py — Pinterest Pin & Comment Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auto‑pins product images to multiple boards and schedules
seasonal content.  Commenting on pins is not supported
by the current Pinterest API and is therefore omitted.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `pinterest_pin_config.json` in the same directory:

{
  "pinterest": {
    "access_token": "YOUR_ACCESS_TOKEN",
    "api_version": "v5"
  },
  "scheduled_pins_file": "pinterest_scheduled_pins.json",
  "state_file": "pinterest_pin_state.json",
  "heartbeat_interval": 30
}

Scheduled pins file (`pinterest_scheduled_pins.json`) – an array of objects:
[
  {
    "image_url": "https://example.com/image.jpg",
    "title": "Stylish Summer Outfit",
    "description": "Discover the latest summer trends.",
    "link": "https://your-store.com/product",
    "board_id": "123456789012345678",
    "scheduled_at": "2025-02-01T12:00:00Z"
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

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "pinterest_pin_bot"
BOT_NAME = "Pinterest Pin & Comment"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "pinterest_pin_config.json"
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
        return {"posted_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Pinterest API helper ─────────────────────────────────────────
PINTEREST_API = "https://api.pinterest.com"

def create_pin(access_token: str, image_url: str, title: str,
               description: str, link: str, board_id: str) -> Optional[str]:
    """Create a pin. Returns the pin ID on success."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {
        "board_id": board_id,
        "media_source": {
            "source_type": "image_url",
            "url": image_url
        },
        "title": title,
        "description": description,
        "link": link
    }
    try:
        resp = requests.post(f"{PINTEREST_API}/v5/pins", json=body, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            data = resp.json()
            return data.get("id")
        else:
            _post(f"Pinterest pin creation failed: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Pinterest API request error: {e}", "error")
        return None

# ── Scheduled pin processing ─────────────────────────────────────
def process_scheduled_pins(config: dict, state: dict):
    """Publish pins that are due."""
    file_path = config.get("scheduled_pins_file", "pinterest_scheduled_pins.json")
    if not os.path.exists(file_path):
        return
    try:
        with open(file_path, "r") as f:
            scheduled = json.load(f)
    except Exception as e:
        _post(f"Error reading scheduled pins file: {e}", "error")
        return

    if not scheduled:
        return

    access_token = config["pinterest"]["access_token"]
    now = datetime.now(timezone.utc)
    remaining = []
    posted_ids = set(state.get("posted_ids", []))

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

        # Generate a unique ID for the scheduled item (to avoid reposting)
        item_id = str(hash(json.dumps(item, sort_keys=True)))
        if item_id in posted_ids:
            continue  # already posted

        if now - timedelta(minutes=1) <= scheduled_dt <= now + timedelta(minutes=1):
            image_url = item.get("image_url")
            title = item.get("title", "")
            description = item.get("description", "")
            link = item.get("link", "")
            board_id = item.get("board_id")

            if not all([image_url, board_id]):
                _post("Skipping incomplete pin entry", "warning")
                continue

            pin_id = create_pin(access_token, image_url, title, description, link, board_id)
            if pin_id:
                _post(f"Pin created: {pin_id} → {title[:50]}", "info", {"pin_id": pin_id})
                posted_ids.add(item_id)
                # success: remove from queue
                continue
            else:
                _post("Pin creation failed, keeping for retry", "error")
        remaining.append(item)

    # Update state
    state["posted_ids"] = list(posted_ids)[-500:]
    # Write remaining items back
    with open(file_path, "w") as f:
        json.dump(remaining, f, indent=2)

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Pinterest Pin & Comment Bot online")
    # Note: Commenting is not available via the public Pinterest API, so only pin scheduling is performed.
    _post("Commenting on pins is not supported by the current Pinterest API; only pin creation is active.", "info")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        access_token = config.get("pinterest", {}).get("access_token")
        if not access_token:
            _post("Pinterest access token missing", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "pinterest_pin_state.json")
        state = load_state(state_file)

        # Process scheduled pins
        process_scheduled_pins(config, state)

        save_state(state_file, state)

        # Poll interval: every 60 seconds to catch pins due within a minute window
        _heartbeat()
        time.sleep(60)

if __name__ == "__main__":
    main()
