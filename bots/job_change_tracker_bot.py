#!/usr/bin/env python3
"""
job_change_tracker_bot.py — Job Change Tracker Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Detects when a target person changes jobs via LinkedIn
(using the Proxycurl API) and alerts the Nazgul
BotController so that sales outreach can be triggered.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `job_change_config.json` in the same directory:

{
  "proxycurl_api_key": "your_api_key",
  "targets": [
    {
      "name": "John Doe",
      "linkedin_url": "https://www.linkedin.com/in/johndoe"
    }
  ],
  "poll_interval_hours": 24,
  "state_file": "job_change_state.json"
}
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "job_change_tracker_bot"
BOT_NAME = "Job Change Tracker"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "job_change_config.json"
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

# ── Proxycurl Person Lookup ─────────────────────────────────────
def fetch_linkedin_profile(linkedin_url: str, api_key: str) -> dict:
    """Fetch current LinkedIn profile data via Proxycurl API."""
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    params = {
        "linkedin_profile_url": linkedin_url,
        "extra": "include",
        "fallback_to_cache": "on-error"
    }
    try:
        resp = requests.get(
            "https://nubela.co/proxycurl/api/v2/linkedin",
            params=params,
            headers=headers,
            timeout=20
        )
        if resp.status_code != 200:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()
    except Exception as e:
        raise Exception(f"Proxycurl error: {e}")

def get_current_job(profile: dict) -> tuple:
    """Extract the current job title and company name from the profile."""
    experiences = profile.get("experiences", [])
    if experiences:
        current_exp = experiences[0]  # first experience is usually current
        title = current_exp.get("title", "")
        company = current_exp.get("company", "")
        return title, company
    return "", ""

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Job Change Tracker Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        api_key = config.get("proxycurl_api_key")
        if not api_key:
            _post("Proxycurl API key missing", "error")
            time.sleep(60)
            continue

        targets = config.get("targets", [])
        poll_hours = float(config.get("poll_interval_hours", 24))
        state_file = config.get("state_file", "job_change_state.json")
        state = load_state(state_file)

        for target in targets:
            name = target.get("name", "Unknown")
            linkedin_url = target.get("linkedin_url")
            if not linkedin_url:
                _post(f"Skipping {name}: no LinkedIn URL", "warning")
                continue

            try:
                profile = fetch_linkedin_profile(linkedin_url, api_key)
            except Exception as e:
                _post(f"Failed to fetch profile for {name}: {e}", "error")
                continue

            new_title, new_company = get_current_job(profile)

            # Compare with stored state
            prev = state.get(linkedin_url, {})
            old_title = prev.get("title", "")
            old_company = prev.get("company", "")

            if old_title != new_title or old_company != new_company:
                # Job change detected!
                payload = {
                    "name": name,
                    "linkedin_url": linkedin_url,
                    "previous_title": old_title,
                    "previous_company": old_company,
                    "new_title": new_title,
                    "new_company": new_company
                }
                summary = f"{name} changed jobs: now {new_title} at {new_company} (was {old_title} at {old_company})"
                _post(summary, "error", payload)  # "error" to signal alert

                # Update state
                state[linkedin_url] = {
                    "title": new_title,
                    "company": new_company,
                    "last_checked": datetime.now(timezone.utc).isoformat()
                }
            else:
                # No change
                _post(f"No job change for {name} ({new_title} at {new_company})", "info")
                state[linkedin_url] = {
                    "title": new_title,
                    "company": new_company,
                    "last_checked": datetime.now(timezone.utc).isoformat()
                }

        save_state(state_file, state)
        _heartbeat()
        # Convert hours to seconds for sleep
        time.sleep(poll_hours * 3600)

if __name__ == "__main__":
    main()
