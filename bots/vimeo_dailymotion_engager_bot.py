#!/usr/bin/env python3
"""
vimeo_dailymotion_engager_bot.py — Vimeo / Dailymotion Niche Engager Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Leaves constructive comments on professional videos from Vimeo and
Dailymotion, linking to relevant case studies. Uses the official APIs
where possible; Playwright fallback is omitted because both platforms
provide HTTP endpoints for commenting.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `vimeo_dailymotion_config.json` in the same directory:

{
  "vimeo": {
    "enabled": true,
    "access_token": "YOUR_VIMEO_ACCESS_TOKEN",
    "search_query": "digital marketing",
    "max_videos": 5,
    "comment_template": "Great video! We recently published a case study on this topic: https://example.com/case-study"
  },
  "dailymotion": {
    "enabled": true,
    "client_id": "YOUR_DAILYMOTION_CLIENT_ID",
    "client_secret": "YOUR_DAILYMOTION_CLIENT_SECRET",
    "refresh_token": "YOUR_DAILYMOTION_REFRESH_TOKEN",
    "username": "your_dailymotion_username",
    "search_query": "digital marketing",
    "max_videos": 5,
    "comment_template": "Great insights! Check out our latest case study: https://example.com/case-study"
  },
  "state_file": "vimeo_dailymotion_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 1440
}
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "vimeo_dailymotion_engager_bot"
BOT_NAME = "Vimeo / Dailymotion Niche Engager"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "vimeo_dailymotion_config.json"
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
        return {"vimeo_commented": [], "dailymotion_commented": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Vimeo ────────────────────────────────────────────────────────
VIMEO_API = "https://api.vimeo.com"

def vimeo_search_videos(access_token: str, query: str, max_videos: int) -> List[str]:
    """Search for recent videos matching the query and return their URIs (video IDs)."""
    headers = {"Authorization": f"bearer {access_token}"}
    params = {
        "query": query,
        "sort": "date",
        "direction": "desc",
        "per_page": min(max_videos, 25)
    }
    try:
        resp = requests.get(f"{VIMEO_API}/videos", headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            videos = data.get("data", [])
            return [v["uri"].split("/")[-1] for v in videos]
        else:
            _post(f"Vimeo search error: {resp.status_code} {resp.text[:200]}", "error")
            return []
    except Exception as e:
        _post(f"Vimeo search request failed: {e}", "error")
        return []

def vimeo_add_comment(access_token: str, video_id: str, text: str) -> bool:
    """Add a comment to a Vimeo video. Returns True on success."""
    headers = {
        "Authorization": f"bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {"text": text}
    try:
        resp = requests.post(f"{VIMEO_API}/videos/{video_id}/comments",
                             headers=headers, json=body, timeout=15)
        if resp.status_code in (200, 201):
            return True
        else:
            _post(f"Vimeo comment error on {video_id}: {resp.status_code} {resp.text[:200]}", "error")
            return False
    except Exception as e:
        _post(f"Vimeo comment request failed: {e}", "error")
        return False

def process_vimeo(config: dict, state: dict):
    """Search Vimeo and comment on new videos."""
    vimeo_cfg = config.get("vimeo", {})
    if not vimeo_cfg.get("enabled", False):
        return

    access_token = vimeo_cfg["access_token"]
    search_query = vimeo_cfg.get("search_query", "")
    max_videos = int(vimeo_cfg.get("max_videos", 5))
    comment_template = vimeo_cfg.get("comment_template", "")
    if not all([access_token, search_query, comment_template]):
        _post("Vimeo configuration incomplete", "warning")
        return

    already_commented = set(state.get("vimeo_commented", []))
    videos = vimeo_search_videos(access_token, search_query, max_videos)
    _post(f"Vimeo: found {len(videos)} videos", "info")

    for video_id in videos:
        if video_id in already_commented:
            continue
        if vimeo_add_comment(access_token, video_id, comment_template):
            _post(f"Vimeo comment added to video {video_id}", "info")
            already_commented.add(video_id)
            state["vimeo_commented"] = list(already_commented)
            time.sleep(1)  # rate limit
        else:
            _post(f"Failed to comment on Vimeo video {video_id}", "error")

# ── Dailymotion ──────────────────────────────────────────────────
DAILYMOTION_API = "https://api.dailymotion.com"

def dailymotion_get_access_token(client_id: str, client_secret: str, refresh_token: str) -> Optional[str]:
    """Exchange refresh token for a new access token."""
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token
    }
    try:
        resp = requests.post(f"{DAILYMOTION_API}/oauth/token", data=data, timeout=15)
        if resp.status_code == 200:
            return resp.json()["access_token"]
        else:
            _post(f"Dailymotion token refresh error: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Dailymotion token refresh failed: {e}", "error")
        return None

def dailymotion_search_videos(access_token: str, query: str, max_videos: int) -> List[str]:
    """Search for videos and return video IDs."""
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "search": query,
        "sort": "recent",
        "limit": min(max_videos, 10)
    }
    try:
        resp = requests.get(f"{DAILYMOTION_API}/videos", headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            return [v["id"] for v in data.get("list", [])]
        else:
            _post(f"Dailymotion search error: {resp.status_code} {resp.text[:200]}", "error")
            return []
    except Exception as e:
        _post(f"Dailymotion search request failed: {e}", "error")
        return []

def dailymotion_add_comment(access_token: str, video_id: str, message: str) -> bool:
    """Add a comment to a Dailymotion video."""
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {"message": message}
    try:
        resp = requests.post(f"{DAILYMOTION_API}/video/{video_id}/comments",
                             headers=headers, json=body, timeout=15)
        if resp.status_code in (200, 201):
            return True
        else:
            _post(f"Dailymotion comment error on {video_id}: {resp.status_code} {resp.text[:200]}", "error")
            return False
    except Exception as e:
        _post(f"Dailymotion comment request failed: {e}", "error")
        return False

def process_dailymotion(config: dict, state: dict):
    """Search Dailymotion and comment on new videos."""
    dm_cfg = config.get("dailymotion", {})
    if not dm_cfg.get("enabled", False):
        return

    client_id = dm_cfg.get("client_id")
    client_secret = dm_cfg.get("client_secret")
    refresh_token = dm_cfg.get("refresh_token")
    search_query = dm_cfg.get("search_query", "")
    max_videos = int(dm_cfg.get("max_videos", 5))
    comment_template = dm_cfg.get("comment_template", "")
    if not all([client_id, client_secret, refresh_token, search_query, comment_template]):
        _post("Dailymotion configuration incomplete", "warning")
        return

    access_token = dailymotion_get_access_token(client_id, client_secret, refresh_token)
    if not access_token:
        return

    already_commented = set(state.get("dailymotion_commented", []))
    videos = dailymotion_search_videos(access_token, search_query, max_videos)
    _post(f"Dailymotion: found {len(videos)} videos", "info")

    for video_id in videos:
        if video_id in already_commented:
            continue
        if dailymotion_add_comment(access_token, video_id, comment_template):
            _post(f"Dailymotion comment added to video {video_id}", "info")
            already_commented.add(video_id)
            state["dailymotion_commented"] = list(already_commented)
            time.sleep(1)
        else:
            _post(f"Failed to comment on Dailymotion video {video_id}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Vimeo / Dailymotion Niche Engager Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "vimeo_dailymotion_state.json")
        state = load_state(state_file)

        process_vimeo(config, state)
        process_dailymotion(config, state)

        save_state(state_file, state)

        poll_min = int(config.get("poll_interval_minutes", 1440))  # daily by default
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
