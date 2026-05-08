#!/usr/bin/env python3
"""
ticket_scalping_bot.py — Primary vs Resale Arbitrage Monitor
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors primary ticket listings and compares them to resale
prices to find profitable flips.

⚡ Requires Playwright for JavaScript‑heavy ticket sites.
⚡ You must configure events and selectors in `ticket_config.json`.

INSTALL
    pip install requests playwright
    python -m playwright install chromium

CONFIG
    Create `ticket_config.json` (see bottom of file).
    This bot will monitor the listed events every scan cycle.
"""

import json
import os
import re
import time
import random
import requests
from datetime import datetime

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "ticket_scalping_bot"
BOT_NAME = "Ticket Scalping Monitor"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ticket_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "ticket_state.json")

SCAN_INTERVAL      = 600   # 10 minutes between full scans
HEARTBEAT_INTERVAL = 20
_last_hb = 0.0

# ── Hub helpers ────────────────────────────────────────────────────────────────
def _post(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID, "bot_name": BOT_NAME,
            "summary": summary, "level": level, "payload": payload or {}
        }, timeout=5)
    except Exception:
        pass

def _heartbeat():
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Config & state ─────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        _post("Config missing. Create ticket_config.json.", "error")
        return []
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"seen_opportunities": {}}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Price parsing ──────────────────────────────────────────────────────────────
def clean_price(text):
    if not text:
        return None
    nums = re.findall(r'[\d,]+\.?\d{0,2}', text.replace(",", ""))
    if nums:
        return float(nums[0])
    return None

# ── Playwright scrapers ────────────────────────────────────────────────────────
def scrape_primary_ticketmaster(page, event):
    """
    Navigate to the Ticketmaster event page, select the cheapest (or specified)
    ticket type and return (price, section, row, available_quantity).
    Expects event dict with 'primary_url' and optional selectors.
    """
    url = event["primary_url"]
    _post(f"Loading primary page: {url}", "info")
    page.goto(url, wait_until="networkidle", timeout=60000)
    # Handle cookie banners / overlays
    try:
        page.click("button:has-text('Accept All Cookies')", timeout=5000)
    except:
        pass
    # Sometimes Ticketmaster shows a sitelink / bot check; if we see "Let's make sure you're not a bot" we can't proceed.
    if page.locator("text=Let's make sure you're not a bot").count():
        _post("Bot check detected on Ticketmaster. Try using a proxy or wait.", "error")
        return None

    # Try to find ticket list. Ticketmaster uses a seat map or list view.
    # If the event uses a list of tickets, we can parse it directly.
    # Common selector for ticket rows:
    ticket_rows = page.locator("li.quick-pick-item, div.seat-list-item, tr.listing")
    if not ticket_rows.count():
        # Maybe it's a map only — too complex, skip
        _post("No ticket list found; may require map interaction. Skipping.", "warning")
        return None

    cheapest = None
    best_row = None
    for i in range(min(ticket_rows.count(), 20)):
        row_elem = ticket_rows.nth(i)
        # Extract price
        price_elem = row_elem.locator("[data-tracking='price'], .price, .amount")
        price_text = price_elem.inner_text() if price_elem.count() else ""
        price = clean_price(price_text)
        if not price:
            continue
        # Extract section/row
        section_text = ""
        row_text = ""
        details = row_elem.locator(".seat-details, .listing-details")
        if details.count():
            details_text = details.inner_text()
            # e.g. "Section 101, Row K"
            section_match = re.search(r'Section\s*(\S+)', details_text, re.IGNORECASE)
            row_match = re.search(r'Row\s*(\S+)', details_text, re.IGNORECASE)
            if section_match: section_text = section_match.group(1)
            if row_match: row_text = row_match.group(1)
        if cheapest is None or price < cheapest:
            cheapest = price
            best_row = f"Sec{section_text} Row{row_text}"
    if cheapest is None:
        _post("No tickets found or price extraction failed.", "warning")
        return None
    # For simplicity, assume quantity = 1 per listing (you can extend)
    return {"price": cheapest, "location": best_row, "quantity": 1}

def scrape_resale_stubhub(page, event):
    """
    Search StubHub for same event and return the lowest resale price
    for a comparable ticket (same section if possible).
    Returns (price, section, row, url) or None.
    """
    url = event["resale_url"]
    _post(f"Loading resale page: {url}", "info")
    page.goto(url, wait_until="networkidle", timeout=60000)
    # StubHub often loads list of tickets
    listings = page.locator("div[data-index], div[class*='TicketContainer']")
    if not listings.count():
        _post("No resale listings found.", "warning")
        return None

    lowest_price = None
    best_location = None
    best_url = url
    for i in range(min(listings.count(), 20)):
        item = listings.nth(i)
        price_elem = item.locator("[data-testid='price'], .sc-hLBbgP, .price")
        price_text = price_elem.inner_text() if price_elem.count() else ""
        price = clean_price(price_text)
        if not price:
            continue
        # Extract section/row
        section_text = ""
        row_text = ""
        locator = item.locator(".sc-fyVfxW, .section-row, .location-info")
        if locator.count():
            loc_text = locator.inner_text()
            section_match = re.search(r'Sec\s*(\S+)', loc_text, re.IGNORECASE)
            row_match = re.search(r'Row\s*(\S+)', loc_text, re.IGNORECASE)
            if section_match: section_text = section_match.group(1)
            if row_match: row_text = row_match.group(1)
        # Optionally, we could try to match the same section from primary,
        # but for now we just record the lowest.
        if lowest_price is None or price < lowest_price:
            lowest_price = price
            best_location = f"Sec{section_text} Row{row_text}"
    if lowest_price is not None:
        return {"price": lowest_price, "location": best_location, "url": best_url}
    return None

# ── Scan logic ─────────────────────────────────────────────────────────────────
def scan():
    config = load_config()
    if not config:
        return
    events = config.get("events", [])
    if not events:
        _post("No events configured.", "warning")
        return

    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return

    state = load_state()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Use a realistic context to reduce bot detection
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()

        for event in events:
            name = event.get("name", event.get("primary_url", "unknown"))
            _post(f"Checking {name}...", "info")

            try:
                primary = scrape_primary_ticketmaster(page, event)
                if not primary:
                    _post(f"Could not get primary price for {name}", "warning")
                    continue

                # Random delay to mimic human
                time.sleep(random.uniform(2, 5))

                resale = scrape_resale_stubhub(page, event)
                if not resale:
                    _post(f"No resale data for {name}", "info")
                    continue

                # Calculate potential profit
                cost = primary["price"]
                resale_price = resale["price"]
                # Assume typical fees: primary ~15%, resale ~10%
                primary_fee_rate = float(event.get("primary_fee_rate", 0.15))
                resale_fee_rate = float(event.get("resale_fee_rate", 0.10))
                total_cost = cost * (1 + primary_fee_rate)
                net_revenue = resale_price * (1 - resale_fee_rate)
                profit = net_revenue - total_cost
                profit_pct = (profit / total_cost) * 100 if total_cost > 0 else 0

                payload = {
                    "event": name,
                    "primary_price": cost,
                    "primary_location": primary["location"],
                    "resale_price": resale_price,
                    "resale_location": resale["location"],
                    "profit": round(profit, 2),
                    "profit_pct": round(profit_pct, 1)
                }

                # Dedup: only alert if profit is positive and new
                opp_id = f"{name}_{primary['location']}_{cost}_{resale_price}"
                if profit >= float(event.get("min_profit", 10)):
                    if opp_id not in state.get("seen_opportunities", {}):
                        _post(f"💰 ARBITRAGE: {name} — Buy @ ${cost:.0f} ({primary['location']}), "
                              f"Sell @ ${resale_price:.0f} ({resale['location']}) → +${profit:.0f} ({profit_pct:.0f}%)",
                              "error" if profit > 50 else "warning", payload)
                        state.setdefault("seen_opportunities", {})[opp_id] = datetime.utcnow().isoformat()
                else:
                    _post(f"{name}: Spread too thin — profit ${profit:.2f}", "info", payload)

                time.sleep(random.uniform(5, 10))
            except Exception as e:
                _post(f"Error with {name}: {e}", "error")
            finally:
                _heartbeat()
        browser.close()

    state["last_scan"] = datetime.utcnow().isoformat()
    save_state(state)

def main():
    _wait_for_hub()
    _post("Ticket Scalping Monitor online — scanning primary vs resale markets.", "info")
    while True:
        scan()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `ticket_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "events": [
    {
      "name": "Taylor Swift - Los Angeles",
      "primary_url": "https://www.ticketmaster.com/event/0B005D48C8B91E2F",
      "resale_url": "https://www.stubhub.com/taylor-swift-tickets/performer/1500142/",
      "primary_fee_rate": 0.15,
      "resale_fee_rate": 0.10,
      "min_profit": 50
    }
  ]
}
"""
