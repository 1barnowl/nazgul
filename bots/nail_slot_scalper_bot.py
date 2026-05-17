#!/usr/bin/env python3
"""
nail_slot_scalper_bot.py — Nail Appointment Re‑seller Bot (Scalper)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors busy nail salons (via Vagaro) for last‑minute cancellations.
When a slot opens up, it posts a high‑priority alert to the BotController
hub so you can resell it to followers for a $10‑$15 fee.

Real data — no simulation. Works with any Vagaro‑powered salon.
Does NOT auto‑book, but the alert contains everything needed to instantly
reserve it manually or wire it into your own checkout flow.

Requirements:
    pip install requests

Configuration:
    A file named `nail_scalper_config.json` will be created on first run.
    Edit it to list the salon slugs and the services you want to watch.
"""

import json
import os
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta

# ── Hub connection ────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "nail_slot_scalper"
BOT_NAME = "Nail Slot Scalper"

# ── Config file ───────────────────────────────────────────────────────────────
CFG_FILE = Path(__file__).with_name("nail_scalper_config.json")

DEFAULT_CONFIG = {
    "salons": [
        {
            "slug": "the-nail-lounge-san-francisco",        # Change to real salons
            "name": "The Nail Lounge",
            "services": ["Manicure", "Pedicure", "Gel Manicure"]
        },
        {
            "slug": "gloss-nail-bar-oakland",
            "name": "Gloss Nail Bar",
            "services": ["Classic Manicure", "Spa Pedicure"]
        }
    ],
    "lookahead_days": 3,            # How many days ahead to scan
    "scan_interval_minutes": 5,     # How often to check for new slots
    "cancellation_alert_level": "error"  # Highest urgency to the hub
}

STATE_FILE = Path(__file__).with_name("nail_slot_state.json")

# ── Hub posting ───────────────────────────────────────────────────────────────
def post_to_hub(summary, level="info", payload=None):
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

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Vagaro availability API ─────────────────────────────────────────────────
# Vagaro uses this endpoint for their public booking widget.
# We replicate the exact payload their widget sends.
VAGARO_API = "https://www.vagaro.com/api/search/availability"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Origin": "https://www.vagaro.com",
    "Referer": "https://www.vagaro.com/",
}

def get_vagaro_availability(salon_slug, service_name, lookahead_days=3):
    """Return a list of available slots as {'date': 'YYYY-MM-DD', 'time': 'HH:MM'}."""
    # Services must be fetched first; we can map name -> id using another call.
    # Simpler: use the search endpoint with a dummy employee (0 = any) and it returns all.
    today = datetime.today()
    end_date = today + timedelta(days=lookahead_days)
    start_str = today.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    payload = {
        "BusinessSlug": salon_slug,
        "EmployeeId": 0,           # Any employee
        "ServiceIds": [],          # We'll fill after getting service IDs
        "StartDate": start_str,
        "EndDate": end_str,
        "TimeZoneOffset": 0
    }

    # First, get the list of services for the salon (to find service ID by name)
    try:
        resp = requests.get(
            f"https://www.vagaro.com/{salon_slug}/services",
            headers={"User-Agent": HEADERS["User-Agent"]},
            timeout=10
        )
        if resp.status_code != 200:
            return []
        # The page contains a global JS variable "window.Vagaro.Services = {...}"
        # Parse it with a quick regex
        import re
        match = re.search(r'window\.Vagaro\.Services\s*=\s*(\[.*?\]);', resp.text, re.DOTALL)
        if not match:
            return []
        services_data = json.loads(match.group(1))
        service_id = None
        for svc in services_data:
            if svc["Name"].strip().lower() == service_name.strip().lower():
                service_id = svc["Id"]
                break
        if not service_id:
            return []   # service not found
    except Exception:
        return []

    payload["ServiceIds"] = [service_id]

    try:
        r = requests.post(VAGARO_API, json=payload, headers=HEADERS, timeout=10)
        r.raise_for_status()
        data = r.json()
        slots = []
        for day_block in data.get("Days", []):
            date = day_block.get("Date")[:10]  # yyyy-mm-dd
            for slot in day_block.get("TimeSlots", []):
                if slot.get("IsAvailable", False):
                    slots.append({"date": date, "time": slot["Time"]})
        return slots
    except Exception:
        return []

# ── State management ──────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Main scanner ─────────────────────────────────────────────────────────────
def scan_all(config, state):
    alert_level = config.get("cancellation_alert_level", "error")
    new_slots_total = 0

    for salon in config["salons"]:
        slug = salon["slug"]
        salon_name = salon["name"]
        services = salon["services"]
        lookahead = config.get("lookahead_days", 3)

        # Ensure state key
        if slug not in state:
            state[slug] = {}

        for svc in services:
            current_slots = get_vagaro_availability(slug, svc, lookahead)
            # Build a set of unique slot identifiers "date|time"
            current_set = {f"{s['date']}|{s['time']}" for s in current_slots}
            previous_set = set(state[slug].get(svc, []))

            # Cancellations = slots that appear now but were not seen before
            new_slots = current_set - previous_set

            if new_slots:
                new_slots_list = sorted([
                    {"date": s.split("|")[0], "time": s.split("|")[1]}
                    for s in new_slots
                ])
                post_to_hub(
                    f"💅 {salon_name} — NEW opening for {svc}!",
                    alert_level,
                    {
                        "salon": salon_name,
                        "slug": slug,
                        "service": svc,
                        "slots": new_slots_list,
                        "booking_link": f"https://www.vagaro.com/{slug}/services",
                        "message": "Cancellation detected — slot open. Sell it fast!"
                    }
                )
                new_slots_total += len(new_slots)

            # Update state with current slots
            state[slug][svc] = list(current_set)

    if new_slots_total > 0:
        post_to_hub(
            f"⏰ {new_slots_total} last‑minute slot(s) found across all salons.",
            "info",
            {"total": new_slots_total}
        )

    save_state(state)

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config file created at {CFG_FILE}. Please edit it with real salon slugs and restart.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    post_to_hub(
        f"Nail Slot Scalper online — watching {len(config['salons'])} salon(s).",
        "info"
    )

    interval_minutes = config.get("scan_interval_minutes", 5)
    # Load previous state
    state = load_state()

    while True:
        try:
            scan_all(config, state)
        except Exception as e:
            post_to_hub(f"Scan error: {e}", "error")
        time.sleep(interval_minutes * 60)

if __name__ == "__main__":
    main()
