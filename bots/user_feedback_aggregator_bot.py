#!/usr/bin/env python3
"""
user_feedback_aggregator_bot.py — User Feedback Aggregator Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Listens for human corrections in the BotController message
stream (e.g. "This lead is spam") and adds those examples
to a training set for subsequent model retraining.

Attachable to the Nazgul BotController (http://localhost:8765).

Configuration
─────────────
Place `feedback_aggregator_config.json` in the same directory:

{
  "db_path": "/path/to/botcontroller.db",
  "lookback_minutes": 120,
  "keywords": ["spam", "correction", "not interested", "false positive"],
  "payload_filter": {
    "type": "correction"
  },
  "training_file": "training_set.jsonl",
  "state_file": "feedback_aggregator_state.json",
  "poll_interval": 60
}

Requirements
────────────
    pip install requests
"""

import json
import os
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "user_feedback_aggregator_bot"
BOT_NAME = "User Feedback Aggregator"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "feedback_aggregator_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id":   BOT_ID,
            "bot_name": BOT_NAME,
            "summary":  summary,
            "level":    level,
            "payload":  payload or {},
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
            "status":   "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> int:
    """Load last processed message ID (0 if none)."""
    try:
        with open(state_file, "r") as f:
            data = json.load(f)
            return data.get("last_id", 0)
    except Exception:
        return 0

def save_state(state_file: str, last_id: int) -> None:
    with open(state_file, "w") as f:
        json.dump({"last_id": last_id, "updated": datetime.now(timezone.utc).isoformat()}, f)

# ── Database reading ─────────────────────────────────────────────
def fetch_messages_since(db_path: str, since_id: int) -> list[dict]:
    """Return messages with id > since_id (ordered by id)."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT id, bot_id, summary, payload, level, ts FROM messages WHERE id > ? ORDER BY id",
            (since_id,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        _post(f"DB read error: {e}", "warning")
        return []

# ── Filter: does the message look like a human correction? ─────
def is_correction(msg: dict, keywords: list[str], payload_filter: dict) -> bool:
    summary = (msg["summary"] or "").lower()
    # keyword match in summary
    if any(kw.lower() in summary for kw in keywords):
        return True
    # payload filter (if any)
    if msg["payload"] and payload_filter:
        try:
            payload = json.loads(msg["payload"])
            if all(payload.get(k) == v for k, v in payload_filter.items()):
                return True
        except Exception:
            pass
    return False

def extract_example(msg: dict, keywords: list[str]) -> dict | None:
    """
    Build a labelled example from the message.
    Priority: payload.text + payload.label → directly.
    Otherwise: summary is the text, label derived from matching keyword.
    """
    payload_text = None
    payload_label = None
    if msg["payload"]:
        try:
            pl = json.loads(msg["payload"])
            payload_text = pl.get("text")
            payload_label = pl.get("label")
        except Exception:
            pass

    text = payload_text if payload_text else msg["summary"]
    if not text:
        return None

    label = payload_label
    if not label:
        # Determine label from summary keywords
        summary_lower = (msg["summary"] or "").lower()
        for kw in keywords:
            if kw.lower() in summary_lower:
                label = kw.lower()
                break
        if not label:
            label = "correction"   # fallback
    return {
        "text": text,
        "label": label,
        "source_bot": msg["bot_id"],
        "timestamp": msg["ts"]
    }

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("User Feedback Aggregator Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        db_path = config.get("db_path")
        if not db_path or not os.path.exists(db_path):
            _post(f"Database not found: {db_path}", "error")
            time.sleep(60)
            continue

        keywords = config.get("keywords", ["spam", "correction"])
        payload_filter = config.get("payload_filter", {})
        training_file = config.get("training_file", "training_set.jsonl")
        state_file = config.get("state_file", "feedback_aggregator_state.json")
        poll_interval = int(config.get("poll_interval", 60))

        last_id = load_state(state_file)
        messages = fetch_messages_since(db_path, last_id)
        new_examples = []

        for msg in messages:
            if is_correction(msg, keywords, payload_filter):
                example = extract_example(msg, keywords)
                if example:
                    new_examples.append(example)

        if new_examples:
            # Append to training file
            try:
                with open(training_file, "a") as f:
                    for ex in new_examples:
                        f.write(json.dumps(ex) + "\n")
                _post(f"Added {len(new_examples)} correction examples to training set",
                      "info", {"count": len(new_examples)})
            except Exception as e:
                _post(f"Failed to write training file: {e}", "error")

        # Update state to the highest id processed
        if messages:
            last_id = max(m["id"] for m in messages)
            save_state(state_file, last_id)

        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
