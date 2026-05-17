#!/usr/bin/env python3
"""
restock_alert_bot.py — Sephora/Ulta Restock Alert Service
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors sold‑out viral products (Rare Beauty, Sol de
Janeiro, etc.) and sends instant SMS alerts to subscribers
via Twilio when they come back in stock.

Pure subscription income – no product handling.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install playwright twilio requests
    playwright install chromium

Configuration
─────────────
Place `restock_alert_config.json` in the same directory:

{
  "twilio": {
    "account_sid": "AC...",
    "auth_token": "your_auth_token",
    "from_number": "+1234567890"
  },
  "products": [
    {
      "id": "rare_beauty_blush",
      "name": "Rare Beauty Soft Pinch Liquid Blush – Happy",
      "url": "https://www.sephora.com/product/rare-beauty-soft-pinch-liquid-blush-P123456",
      "stock_selector": "button[data-test='add-to-bag-button']",   // visible when in stock
      "out_of_stock_selector": "button[data-test='out-of-stock-button']" // visible when OOS
    },
    {
      "id": "sol_de_janeiro_mist",
      "name": "Sol de Janeiro Brazilian Crush Cheirosa 62 Perfume Mist",
      "url": "https://www.ulta.com/p/brazilian-crush-cheirosa-62-perfume-mist-x12345",
      "stock_selector": "button:has-text('Add to Bag')",
      "out_of_stock_selector": "span:has-text('Out of Stock')"
    }
  ],
  "subscribers_file": "restock_subscribers.json",
  "poll_interval_seconds": 300,
  "state_file": "restock_alert_state.json",
  "heartbeat_interval": 30
}

Subscribers file (`restock_subscribers.json`) – array of objects:
[
  {
    "phone": "+19876543210",
    "products": ["rare_beauty_blush", "sol_de_janeiro_mist"],
    "active": true
  },
  ...
]
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from playwright.sync_api import sync_playwright, Page
from twilio.rest import Client as TwilioClient

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "restock_alert_bot"
BOT_NAME = "Sephora/Ulta Restock Alert"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "restock_alert_config.json"
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

# ── State management ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"stock_status": {}}   # product_id -> bool (true = in stock)

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

def load_subscribers(subscribers_file: str) -> List[dict]:
    try:
        with open(subscribers_file, "r") as f:
            return json.load(f)
    except Exception:
        return []

# ── Twilio SMS ──────────────────────────────────────────────────
def send_sms(account_sid: str, auth_token: str, from_number: str,
             to: str, body: str) -> bool:
    try:
        client = TwilioClient(account_sid, auth_token)
        msg = client.messages.create(
            body=body,
            from_=from_number,
            to=to
        )
        return msg.sid is not None
    except Exception as e:
        _post(f"Twilio error: {e}", "error")
        return False

# ── Stock checking via Playwright ────────────────────────────────
def check_stock(page: Page, product: dict) -> Optional[bool]:
    """Return True if in stock, False if out of stock, None if uncertain."""
    url = product["url"]
    in_stock_sel = product.get("stock_selector")
    oos_sel = product.get("out_of_stock_selector")
    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
        # First look for the "in stock" indicator (button to add to cart)
        if in_stock_sel:
            elem = page.query_selector(in_stock_sel)
            if elem and elem.is_visible():
                return True
        # Then check for the "out of stock" indicator
        if oos_sel:
            elem = page.query_selector(oos_sel)
            if elem and elem.is_visible():
                return False
        # If neither found, try common phrases
        content = page.content().lower()
        if "add to bag" in content or "add to cart" in content:
            return True
        if "out of stock" in content or "sold out" in content:
            return False
        return None
    except Exception as e:
        _post(f"Error checking {product['name']}: {e}", "warning")
        return None

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Sephora/Ulta Restock Alert Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        twilio_cfg = config.get("twilio", {})
        account_sid = twilio_cfg.get("account_sid")
        auth_token = twilio_cfg.get("auth_token")
        from_number = twilio_cfg.get("from_number")
        if not all([account_sid, auth_token, from_number]):
            _post("Twilio credentials missing", "error")
            time.sleep(300)
            continue

        products = config.get("products", [])
        if not products:
            _post("No products configured", "error")
            time.sleep(300)
            continue

        subscribers_file = config.get("subscribers_file", "restock_subscribers.json")
        state_file = config.get("state_file", "restock_alert_state.json")
        poll_interval = int(config.get("poll_interval_seconds", 300))

        state = load_state(state_file)
        stock_status = state.setdefault("stock_status", {})

        subscribers = load_subscribers(subscribers_file)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            for product in products:
                prod_id = product["id"]
                prod_name = product["name"]
                new_status = check_stock(page, product)
                if new_status is None:
                    _post(f"Could not determine stock for {prod_name}", "warning")
                    continue
                previous = stock_status.get(prod_id)
                stock_status[prod_id] = new_status

                if previous is False and new_status is True:
                    # Product went from OOS to in stock – alert subscribers
                    _post(f"BACK IN STOCK: {prod_name}", "warning", {"product": prod_name})

                    # Find subscribers for this product
                    alerted = []
                    for sub in subscribers:
                        if sub.get("active", True) and prod_id in sub.get("products", []):
                            phone = sub.get("phone")
                            if phone:
                                sms_body = f"🚀 Restock Alert! {prod_name} is back in stock! Grab it before it's gone: {product['url']}"
                                success = send_sms(account_sid, auth_token, from_number, phone, sms_body)
                                if success:
                                    alerted.append(phone)
                                    _post(f"Alert sent to {phone}", "info")
                                else:
                                    _post(f"Failed to alert {phone}", "error")
                                time.sleep(1)  # Twilio rate limit
                    _post(f"Alerts sent to {len(alerted)} subscribers for {prod_name}", "info")
                elif previous is None and new_status is False:
                    # First check, product is OOS – just record
                    _post(f"Product {prod_name} is currently out of stock", "info")
                elif new_status is True:
                    _post(f"Product {prod_name} is in stock (already reported)", "info")
                # Prevent notifying if changed from in stock to out of stock? We'll just update status silently.

            browser.close()

        save_state(state_file, state)
        _heartbeat()
        time.sleep(poll_interval)

if __name__ == "__main__":
    main()
