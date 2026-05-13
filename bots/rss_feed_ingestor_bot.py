#!/usr/bin/env python3
"""
rss_feed_ingestor_bot.py — RSS/Atom Feed Ingestor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors thousands of RSS/Atom feeds for real‑time content
updates. New entries are reported to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install feedparser requests

Configuration
─────────────
Place `feed_ingestor_config.json` in the same directory:

{
  "feeds": [
    {
      "url": "https://example.com/rss",
      "name": "Example Blog"
    }
  ],
  "user_agent": "Mozilla/5.0 (compatible; BotControllerRSS/1.0)",
  "fetch_timeout": 20,
  "poll_interval_minutes": 30,
  "state_file": "feed_ingestor_state.json",
  "max_entries_per_fetch": 20
}
"""

import json
import os
import time
import hashlib
from datetime import datetime, timezone, timedelta
from pathlib import Path

import feedparser
import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "rss_feed_ingestor_bot"
BOT_NAME = "RSS/Atom Feed Ingestor"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "feed_ingestor_config.json"
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
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Entry fingerprinting ─────────────────────────────────────────
def entry_fingerprint(entry: dict) -> str:
    """Create a unique hash for an entry."""
    raw = entry.get("id") or entry.get("link") or ""
    if not raw:
        # fallback: title + published date
        title = entry.get("title", "")
        published = entry.get("published", "") or entry.get("updated", "")
        raw = title + "|" + published
    return hashlib.sha256(raw.encode()).hexdigest()

# ── Feed fetching ────────────────────────────────────────────────
def fetch_feed(feed_cfg: dict, user_agent: str, timeout: int,
               max_entries: int) -> list[dict]:
    """Fetch and parse feed, return list of new entries."""
    url = feed_cfg["url"]
    name = feed_cfg.get("name", url)
    try:
        resp = requests.get(url, timeout=timeout,
                            headers={"User-Agent": user_agent})
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception as e:
        _post(f"Failed to fetch feed {name}: {e}", "warning")
        return []
    if feed.bozo:
        _post(f"Feed {name} may be malformed: {feed.bozo_exception}", "warning")
    entries = feed.entries
    if max_entries and len(entries) > max_entries:
        entries = entries[:max_entries]
    return entries

def get_new_entries(entries: list[dict], feed_url: str,
                    state: dict) -> list[dict]:
    """Return entries not yet seen for this feed."""
    seen_hashes = set(state.get(feed_url, []))
    new_entries = []
    for entry in entries:
        fp = entry_fingerprint(entry)
        if fp not in seen_hashes:
            new_entries.append(entry)
            seen_hashes.add(fp)
    # Update state with new hashes (we'll save later)
    state[feed_url] = list(seen_hashes)
    return new_entries

def build_entry_payload(entry: dict, feed_name: str) -> dict:
    """Create a minimal summary and payload for a new entry."""
    title = entry.get("title", "No title")
    link = entry.get("link", "")
    published = entry.get("published", "") or entry.get("updated", "")
    summary_text = (entry.get("summary") or entry.get("description", ""))[:200]
    return {
        "feed_name": feed_name,
        "title": title,
        "link": link,
        "published": published,
        "summary": summary_text,
        "author": entry.get("author", ""),
        "tags": [t.get("term") for t in entry.get("tags", [])] if entry.get("tags") else []
    }

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("RSS/Atom Feed Ingestor Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        feeds = config.get("feeds", [])
        user_agent = config.get("user_agent", "Mozilla/5.0 (compatible; RSSBot/1.0)")
        timeout = int(config.get("fetch_timeout", 20))
        poll_interval = int(config.get("poll_interval_minutes", 30)) * 60
        max_entries = int(config.get("max_entries_per_fetch", 20))
        state_file = config.get("state_file", "feed_ingestor_state.json")
        state = load_state(state_file)

        total_new = 0
        for feed_cfg in feeds:
            entries = fetch_feed(feed_cfg, user_agent, timeout, max_entries)
            if not entries:
                continue
            feed_url = feed_cfg["url"]
            feed_name = feed_cfg.get("name", feed_url)
            new_entries = get_new_entries(entries, feed_url, state)
            for entry in new_entries:
                payload = build_entry_payload(entry, feed_name)
                _post(f"New entry from {feed_name}: {payload['title']}", "info", payload)
                total_new += 1
                _heartbeat()  # keep connection alive during large batches

        save_state(state_file, state)
        if total_new > 0:
            _post(f"Processed {total_new} new entries across {len(feeds)} feeds", "info")
        else:
            _post("No new entries", "info")

        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
