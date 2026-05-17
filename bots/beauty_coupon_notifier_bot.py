#!/usr/bin/env python3
"""
beauty_coupon_notifier_bot.py — Beauty Coupon & Cashback Notifier Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Trawls coupon sites (via CouponAPI) for working discount codes,
then posts them to a Telegram channel with your affiliate link.
Earns cashback commission when users click and buy.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests python-telegram-bot

Configuration
─────────────
Place `beauty_coupon_notifier_config.json` in the same directory:

{
  "coupon_api": {
    "api_key": "YOUR_COUPONAPI_API_KEY",          // get a free key at https://www.couponapi.com/
    "base_url": "https://api.couponapi.com/v1"
  },
  "telegram": {
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "chat_id": "@your_channel_or_group_id"        // e.g., "@beautydeals"
  },
  "stores": [
    "sephora", "ulta", "beautybay", "lookfantastic", "cultbeauty"
  ],
  "affiliate": {
    "tag": "?ref=your_affiliate_id"               // appended to each store's homepage
  },
  "state_file": "beauty_coupon_notifier_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 1440                   // daily (CouponAPI free tier: 50 req/day)
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
import telegram

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "beauty_coupon_notifier_bot"
BOT_NAME = "Beauty Coupon & Cashback Notifier"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "beauty_coupon_notifier_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(
            f"{HUB}/ingest",
            json={
                "bot_id": BOT_ID,
                "bot_name": BOT_NAME,
                "summary": summary,
                "level": level,
                "payload": payload or {},
            },
            timeout=5,
        )
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(
            f"{HUB}/heartbeat/{BOT_ID}",
            json={"bot_name": BOT_NAME, "status": "online"},
            timeout=3,
        )
    except Exception:
        pass
    _last_hb = time.time()

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"posted_coupon_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── CouponAPI ────────────────────────────────────────────────────
def fetch_coupons(api_key: str, store: str, base_url: str) -> List[dict]:
    """Get active coupon codes for a given store from CouponAPI."""
    headers = {"Authorization": f"Bearer {api_key}"}
    params = {"search": store}
    try:
        resp = requests.get(f"{base_url}/get_coupons", headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            # The API returns a list of coupon objects under "coupons" key? The docs show direct list.
            coupons = data if isinstance(data, list) else data.get("coupons", [])
            return coupons
        else:
            _post(f"CouponAPI error for {store}: {resp.status_code} {resp.text[:200]}", "error")
            return []
    except Exception as e:
        _post(f"CouponAPI request failed: {e}", "error")
        return []

def build_affiliate_store_url(store: str, affiliate_tag: str) -> str:
    """Create the affiliate‑wrapped homepage URL."""
    store_domain = store.replace(" ", "").lower() + ".com"
    return f"https://www.{store_domain}{affiliate_tag}"

# ── Telegram notification ────────────────────────────────────────
def send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    try:
        bot = telegram.Bot(token=bot_token)
        bot.send_message(chat_id=chat_id, text=text, parse_mode=telegram.ParseMode.HTML,
                         disable_web_page_preview=False)
        return True
    except Exception as e:
        _post(f"Telegram send error: {e}", "error")
        return False

# ── Main logic ───────────────────────────────────────────────────
def process_coupons(config: dict, state: dict):
    api_key = config["coupon_api"]["api_key"]
    base_url = config["coupon_api"].get("base_url", "https://api.couponapi.com/v1")
    telegram_cfg = config["telegram"]
    stores = config.get("stores", [])
    affiliate_tag = config.get("affiliate", {}).get("tag", "")
    posted_ids = set(state.get("posted_coupon_ids", []))
    new_posted = 0

    if not api_key:
        _post("CouponAPI key missing", "error")
        return

    for store in stores:
        coupons = fetch_coupons(api_key, store, base_url)
        if not coupons:
            continue
        for coupon in coupons:
            coupon_id = coupon.get("id") or str(hash(coupon.get("coupon_code") + coupon.get("store") + coupon.get("title")))
            if coupon_id in posted_ids:
                continue
            # Extract details
            code = coupon.get("coupon_code", "")
            title = coupon.get("title", "Discount")
            description = coupon.get("description", "")
            expiry = coupon.get("expiry_date", "N/A")
            store_name = coupon.get("store", store)
            affiliate_url = build_affiliate_store_url(store_name, affiliate_tag)
            # Build message
            text = (
                f"💄 *{store_name.title()} Deal* 💄\n\n"
                f"*{title}*\n"
                f"Code: `{code}`\n"
                f"Expires: {expiry}\n\n"
                f"👉 [Shop now]({affiliate_url})\n"
                f"_{description}_"
            )
            success = send_telegram_message(telegram_cfg["bot_token"], telegram_cfg["chat_id"], text)
            if success:
                _post(f"Posted coupon: {title[:50]} ({code})", "info", {"store": store_name, "code": code})
                posted_ids.add(coupon_id)
                new_posted += 1
            else:
                _post(f"Failed to post coupon {code}", "error")
            time.sleep(0.5)  # avoid flooding

    # Keep only last 2000 posted IDs
    state["posted_coupon_ids"] = list(posted_ids)[-2000:]
    if new_posted:
        _post(f"Posted {new_posted} new coupon codes", "info")
    else:
        _post("No new coupons found", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Beauty Coupon & Cashback Notifier Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "beauty_coupon_notifier_state.json")
        state = load_state(state_file)

        process_coupons(config, state)
        save_state(state_file, state)

        poll_minutes = int(config.get("poll_interval_minutes", 1440))
        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
