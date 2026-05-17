#!/usr/bin/env python3
"""
wedding_guest_outfit_finder_bot.py — Wedding-Guest Outfit Finder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fetches real product listings from ASOS, Reformation, and Revolve based on
dress code & season. Posts every found item as a clickable affiliate link
to the BotController hub. Commission‑ready – just add your own affiliate IDs.

Requirements:
    pip install requests beautifulsoup4 lxml

Configuration:
    A file named `wedding_config.json` will be created automatically in the
    same folder. Edit it to set dress code, season, and affiliate parameters.
"""

import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Hub connection ─────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "wedding_outfit_finder"
BOT_NAME = "Wedding Outfit Finder"

# ── Configuration file ─────────────────────────────────────────────────────
CFG_FILE = Path(__file__).with_name("wedding_config.json")

DEFAULT_CONFIG = {
    "dress_code": "cocktail",               # e.g., formal, black-tie, casual
    "season":     "spring",                 # e.g., summer, fall, winter
    "affiliate": {
        "asos": {
            "awin_merchant_id": "10943",    # ASOS US – change if needed
            "awinaffid": "12345"            # ← PUT YOUR AWIN PUBLISHER ID HERE
        },
        "reformation": {
            "ref_param": "utm_source",      # generic referral parameter
            "ref_value": "myaffiliate123"
        },
        "revolve": {
            "ref_param": "utm_medium",
            "ref_value": "myaffiliate_revolve"
        }
    },
    "max_items_per_store": 5,
    "scan_interval_hours": 6                # re‑scan every 6 hours, 0 = run once
}

# ── Helper: load / create config ───────────────────────────────────────────
def load_config():
    if CFG_FILE.exists():
        with open(CFG_FILE, "r") as f:
            return json.load(f)
    else:
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        print(f"Default config created at {CFG_FILE}. Edit it and restart.")
        sys.exit(0)

# ── Hub posting (same protocol as momentum_chaser) ─────────────────────────
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
        pass   # hub may be temporarily down

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Generic scraper helpers ────────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
    )
}

def fetch_soup(url, params=None):
    """Return BeautifulSoup object or None."""
    try:
        resp = requests.get(url, headers=HEADERS, params=params, timeout=15)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml")
    except Exception:
        return None

# ── ASOS scraper ───────────────────────────────────────────────────────────
def search_asos(query, config, max_items=5):
    """Return list of {name, price, url, affiliate_link} from ASOS."""
    aff = config["affiliate"]["asos"]
    awin_merchant_id = aff.get("awin_merchant_id", "10943")
    awinaffid = aff.get("awinaffid", "12345")

    # ASOS search URL (US site)
    base_url = "https://www.asos.com/us/search/"
    params = {"q": query, "sort": "relevance"}
    soup = fetch_soup(base_url, params=params)
    if not soup:
        return []

    items = []
    # Product tiles have data-auto-id="productTile"
    for tile in soup.select('[data-auto-id="productTile"]'):
        if len(items) >= max_items:
            break
        try:
            link = tile.find("a", href=True)
            name_elem = tile.select_one('[data-auto-id="productTileDescription"]')
            price_elem = tile.select_one('[data-auto-id="productTilePrice"]')

            if not (link and name_elem):
                continue

            product_url = "https://www.asos.com" + link["href"]
            name = name_elem.get_text(strip=True)
            price = price_elem.get_text(strip=True) if price_elem else "N/A"

            # Build Awin affiliate link
            encoded_url = urllib.parse.quote(product_url, safe="")
            affiliate_link = (
                f"https://www.awin1.com/cread.php?"
                f"awinmid={awin_merchant_id}&awinaffid={awinaffid}&ued={encoded_url}"
            )

            items.append({
                "store": "ASOS",
                "name": name,
                "price": price,
                "url": product_url,
                "affiliate_link": affiliate_link
            })
        except Exception:
            continue
    return items

# ── Reformation scraper ────────────────────────────────────────────────────
def search_reformation(query, config, max_items=5):
    """Return list of {name, price, url, affiliate_link} from Reformation."""
    aff = config["affiliate"]["reformation"]
    ref_param = aff.get("ref_param", "utm_source")
    ref_value = aff.get("ref_value", "myaffiliate123")

    base_url = "https://www.thereformation.com/search"
    params = {"q": query}
    soup = fetch_soup(base_url, params=params)
    if not soup:
        return []

    items = []
    # Product tiles in search results (class may be 'product-tile')
    for tile in soup.select('.product-tile'):
        if len(items) >= max_items:
            break
        try:
            link = tile.find("a", href=True)
            name_elem = tile.select_one('.product-tile__name, .product-name')
            price_elem = tile.select_one('.product-tile__price, .product-price')

            if not link:
                continue
            product_url = link["href"]
            if not product_url.startswith("http"):
                product_url = "https://www.thereformation.com" + product_url

            name = name_elem.get_text(strip=True) if name_elem else "Unknown"
            price = price_elem.get_text(strip=True) if price_elem else "N/A"

            # Build affiliate link with configurable referral param
            separator = "&" if "?" in product_url else "?"
            affiliate_link = f"{product_url}{separator}{ref_param}={ref_value}"

            items.append({
                "store": "Reformation",
                "name": name,
                "price": price,
                "url": product_url,
                "affiliate_link": affiliate_link
            })
        except Exception:
            continue
    return items

# ── Revolve scraper ────────────────────────────────────────────────────────
def search_revolve(query, config, max_items=5):
    """Return list of {name, price, url, affiliate_link} from Revolve."""
    aff = config["affiliate"]["revolve"]
    ref_param = aff.get("ref_param", "utm_medium")
    ref_value = aff.get("ref_value", "myaffiliate_revolve")

    base_url = "https://www.revolve.com/search/"
    soup = fetch_soup(base_url, params={"q": query})
    if not soup:
        return []

    items = []
    # Revolve product tiles may have class 'product-tile' or 'js-product-tile'
    for tile in soup.select('.product-tile, .js-product-tile'):
        if len(items) >= max_items:
            break
        try:
            link = tile.find("a", href=True)
            name_elem = tile.select_one('.product-tile__brand, .product-name')
            # Price may be in .product-tile__price
            price_elem = tile.select_one('.product-tile__price, .product-price')

            if not link:
                continue
            product_url = link["href"]
            if not product_url.startswith("http"):
                product_url = "https://www.revolve.com" + product_url

            name = name_elem.get_text(strip=True) if name_elem else "Unknown"
            price = price_elem.get_text(strip=True) if price_elem else "N/A"

            separator = "&" if "?" in product_url else "?"
            affiliate_link = f"{product_url}{separator}{ref_param}={ref_value}"

            items.append({
                "store": "Revolve",
                "name": name,
                "price": price,
                "url": product_url,
                "affiliate_link": affiliate_link
            })
        except Exception:
            continue
    return items

# ── Main scan routine ──────────────────────────────────────────────────────
def run_scan(config):
    dress_code = config["dress_code"].strip().lower()
    season = config["season"].strip().lower()
    max_items = config.get("max_items_per_store", 5)

    # Build a flexible search query
    query = f"wedding guest {dress_code} dress {season}"

    post_to_hub(
        f"🔍 Searching for “{query}” across ASOS, Reformation & Revolve…",
        "info",
        {"query": query}
    )

    # ASOS
    asos_items = search_asos(query, config, max_items)
    for item in asos_items:
        post_to_hub(
            f"ASOS: {item['name']} — {item['price']}",
            "info",
            item
        )
        time.sleep(0.5)   # gentle rate limit

    # Reformation
    ref_items = search_reformation(query, config, max_items)
    for item in ref_items:
        post_to_hub(
            f"Reformation: {item['name']} — {item['price']}",
            "info",
            item
        )
        time.sleep(0.5)

    # Revolve
    rev_items = search_revolve(query, config, max_items)
    for item in rev_items:
        post_to_hub(
            f"Revolve: {item['name']} — {item['price']}",
            "info",
            item
        )
        time.sleep(0.5)

    total = len(asos_items) + len(ref_items) + len(rev_items)
    post_to_hub(
        f"✅ Scan complete. Found {total} outfit ideas.",
        "info",
        {"total_found": total}
    )

# ── Entry point ────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    config = load_config()
    interval_h = config.get("scan_interval_hours", 6)

    post_to_hub(
        f"Bot online — dress code: {config['dress_code']}, season: {config['season']}",
        "info"
    )

    if interval_h <= 0:
        # Run once and exit
        run_scan(config)
    else:
        # Run periodically
        while True:
            run_scan(config)
            post_to_hub(
                f"⏳ Next scan in {interval_h} hour(s).",
                "info"
            )
            time.sleep(interval_h * 3600)

if __name__ == "__main__":
    main()
