#!/usr/bin/env python3
"""
restock_warmer_bot.py — Out‑of‑Stock Opportunity Warmer Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Detects when a sold‑out item is about to be restocked and
pre‑loads the cart by automatically adding the item. Sends
immediate alerts to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install playwright requests
    playwright install chromium

Configuration
─────────────
Place `restock_warmer_config.json` in the same directory:

{
  "products": [
    {
      "id": "nintendo_switch_oled",
      "name": "Nintendo Switch OLED",
      "url": "https://www.target.com/p/nintendo-switch-oled-model/-/A-...",
      "out_of_stock_selector": "button[data-test='soldOutBlock']",
      "add_to_cart_selector": "button[data-test='addToCartButton']",
      "check_interval_minutes": 2
    },
    {
      "id": "ps5_disc",
      "name": "PlayStation 5 Disc Edition",
      "url": "https://www.walmart.com/ip/PlayStation-5-Console/...",
      "out_of_stock_selector": "span:has-text('Out of stock')",
      "add_to_cart_selector": "button:has-text('Add to cart')",
      "check_interval_minutes": 2
    }
  ],
  "state_file": "restock_warmer_state.json",
  "heartbeat_interval": 30
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
BOT_ID   = "restock_warmer_bot"
BOT_NAME = "Restock Opportunity Warmer"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "restock_warmer_config.json"
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

# ── State management ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Playwright‑based monitor and cart warmer ─────────────────────
def check_and_warm(product: dict, state: dict) -> None:
    product_id = product["id"]
    url = product["url"]
    name = product.get("name", product_id)
    out_of_stock_selector = product.get("out_of_stock_selector")
    add_to_cart_selector = product.get("add_to_cart_selector")

    if not out_of_stock_selector or not add_to_cart_selector:
        _post(f"Missing selectors for {name}", "error")
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _post("Playwright not installed. Install with: pip install playwright && playwright install chromium", "error")
        return

    # Track last state to avoid duplicate actions
    product_state = state.setdefault(product_id, {
        "last_out_of_stock": True,
        "cart_warmed": False
    })

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Check if out-of-stock element is visible
            out_of_stock_elem = page.query_selector(out_of_stock_selector)
            is_out_of_stock = out_of_stock_elem is not None and out_of_stock_elem.is_visible()

            if is_out_of_stock:
                product_state["last_out_of_stock"] = True
                product_state["cart_warmed"] = False
                _post(f"{name}: Still out of stock", "info", {"product_id": product_id})
                browser.close()
                return

            # Not out of stock
            if not product_state["last_out_of_stock"]:
                # Already saw it in stock previously, no new change
                if not product_state["cart_warmed"]:
                    # Cart hasn't been warmed yet, try to add to cart
                    add_btn = page.query_selector(add_to_cart_selector)
                    if add_btn and add_btn.is_visible():
                        add_btn.click()
                        # Wait for cart confirmation
                        page.wait_for_load_state("networkidle", timeout=5000)
                        product_state["cart_warmed"] = True
                        _post(f"{name}: Added to cart (pre‑loaded)", "warning", {"product_id": product_id, "url": url})
                    else:
                        _post(f"{name}: In stock, but add‑to‑cart button not found", "warning", {"product_id": product_id})
                else:
                    # Already warmed
                    _post(f"{name}: Cart already warmed", "info", {"product_id": product_id})
            else:
                # First time we see it in stock after being out of stock
                product_state["last_out_of_stock"] = False
                # Immediately try to add to cart
                add_btn = page.query_selector(add_to_cart_selector)
                if add_btn and add_btn.is_visible():
                    add_btn.click()
                    page.wait_for_load_state("networkidle", timeout=5000)
                    product_state["cart_warmed"] = True
                    _post(f"{name}: BACK IN STOCK! Added to cart (pre‑loaded). BUY NOW!", "error",
                          {"product_id": product_id, "url": url, "name": name})
                else:
                    _post(f"{name}: Back in stock, but add‑to‑cart button missing", "error",
                          {"product_id": product_id, "url": url})

            browser.close()
    except Exception as e:
        _post(f"Error monitoring {name}: {e}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Out‑of‑Stock Opportunity Warmer Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        products = config.get("products", [])
        state_file = config.get("state_file", "restock_warmer_state.json")
        state = load_state(state_file)

        for product in products:
            product_id = product.get("id")
            if not product_id:
                continue
            check_and_warm(product, state)
            save_state(state_file, state)
            _heartbeat()
            # Wait per-product interval
            interval = float(product.get("check_interval_minutes", 5)) * 60
            time.sleep(interval)

        # Loop back after full cycle
        _heartbeat()
        # A small pause before the next full cycle so we don't hammer
        time.sleep(10)

if __name__ == "__main__":
    main()
