#!/usr/bin/env python3
"""
luxury_handbag_broker_bot.py — Luxury Handbag Broker Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans listings for Chanel / Hermès bags being sold
cheaply on eBay and posts them to a Telegram group
with an affiliate link.  Earns commission from the
eBay Partner Network without ever owning the bag.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests python-telegram-bot

Configuration
─────────────
Place `luxury_broker_config.json` in the same directory:

{
  "ebay": {
    "app_id": "YOUR_EBAY_APP_ID",
    "affiliate_campaign_id": "533xxxxxxxx",    // optional, for eBay Partner Network
    "affiliate_geodevice": "p"                  // e.g. "p" for desktop
  },
  "telegram": {
    "bot_token": "YOUR_TELEGRAM_BOT_TOKEN",
    "chat_id": "@your_channel_or_group_id"      // e.g. "@luxurybagdeals" or "-1001234567890"
  },
  "search": {
    "brands": ["Chanel", "Hermès"],
    "max_price": 500,
    "results_per_brand": 5,
    "min_profit_margin_percent": 10              // only show items at least X% below market (optional)
  },
  "state_file": "luxury_broker_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 60
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
BOT_ID = "luxury_handbag_broker_bot"
BOT_NAME = "Luxury Handbag Broker"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "luxury_broker_config.json"
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
        return {"posted_item_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── eBay Finding API ─────────────────────────────────────────────
EBAY_FINDING = "https://svcs.ebay.com/services/search/FindingService/v1"

def ebay_search(ebay_cfg: dict, brand: str, max_price: float, limit: int) -> List[dict]:
    """Return listings for a given brand under the price cap."""
    app_id = ebay_cfg["app_id"]
    params = {
        "OPERATION-NAME": "findItemsAdvanced",
        "SERVICE-VERSION": "1.0.0",
        "SECURITY-APPNAME": app_id,
        "RESPONSE-DATA-FORMAT": "JSON",
        "REST-PAYLOAD": "",
        "keywords": f"{brand} bag",
        "itemFilter(0).name": "MaxPrice",
        "itemFilter(0).value": str(max_price),
        "itemFilter(0).paramName": "Currency",
        "itemFilter(0).paramValue": "USD",
        "paginationInput.entriesPerPage": str(limit),
        "sortOrder": "PricePlusShippingLowest"
    }
    try:
        resp = requests.get(EBAY_FINDING, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            items = data.get("findItemsAdvancedResponse", [{}])[0].get("searchResult", {}).get("item", [])
            return items
        else:
            _post(f"eBay API error: {resp.status_code} {resp.text[:200]}", "error")
            return []
    except Exception as e:
        _post(f"eBay API request failed: {e}", "error")
        return []

def get_affiliate_link(item_id: str, ebay_cfg: dict) -> str:
    """Build an eBay affiliate link using the EPN scheme."""
    campaign = ebay_cfg.get("affiliate_campaign_id")
    geodevice = ebay_cfg.get("affiliate_geodevice", "p")
    base_url = "https://www.ebay.com/itm"
    if campaign:
        return f"https://rover.ebay.com/rover/1/{campaign}/1?ff3=2&toolid=10001&campid={campaign}&customid=&mpre={base_url}/{item_id}&geo_id={geodevice}"
    else:
        # fallback: direct eBay link (no affiliate)
        return f"https://www.ebay.com/itm/{item_id}"

# ── Telegram notification ────────────────────────────────────────
def send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    """Send a message to a Telegram chat using the bot token."""
    try:
        bot = telegram.Bot(token=bot_token)
        bot.send_message(chat_id=chat_id, text=text, parse_mode=telegram.ParseMode.HTML,
                         disable_web_page_preview=False)
        return True
    except Exception as e:
        _post(f"Telegram send error: {e}", "error")
        return False

# ── Main logic ───────────────────────────────────────────────────
def scan_and_post(config: dict, state: dict):
    ebay_cfg = config["ebay"]
    telegram_cfg = config["telegram"]
    search_cfg = config["search"]

    brands = search_cfg.get("brands", [])
    max_price = float(search_cfg.get("max_price", 500))
    limit = int(search_cfg.get("results_per_brand", 5))
    posted_ids = set(state.get("posted_item_ids", []))
    new_posted = 0

    for brand in brands:
        items = ebay_search(ebay_cfg, brand, max_price, limit)
        for item in items:
            item_id = item["itemId"]
            if item_id in posted_ids:
                continue
            title = item["title"]
            price = float(item["sellingStatus"]["currentPrice"]["__value__"])
            link = get_affiliate_link(item_id, ebay_cfg)
            # Prepare message
            message = (
                f"👜 *{brand} Bag Deal*\n\n"
                f"*{title}*\n"
                f"Price: ${price:.2f}\n"
                f"[Buy on eBay]({link})"
            )
            success = send_telegram_message(
                telegram_cfg["bot_token"],
                telegram_cfg["chat_id"],
                message
            )
            if success:
                _post(f"Posted {brand} bag: {title[:60]} (${price:.2f})", "info")
                posted_ids.add(item_id)
                new_posted += 1
            else:
                _post(f"Failed to post {brand} bag", "error")
            time.sleep(1)  # avoid flooding

    # Keep only last 2000 posted IDs
    state["posted_item_ids"] = list(posted_ids)[-2000:]
    if new_posted:
        _post(f"Posted {new_posted} new luxury bag deals", "info")
    else:
        _post("No new bag deals to post", "info")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Luxury Handbag Broker Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "luxury_broker_state.json")
        state = load_state(state_file)

        scan_and_post(config, state)
        save_state(state_file, state)

        poll_minutes = int(config.get("poll_interval_minutes", 60))
        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
