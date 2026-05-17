#!/usr/bin/env python3
"""
baby_gear_flash_sale_affiliate_bot.py — Baby‑Gear Flash‑Sale Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors the official sale pages of Snoo (Happiest Baby) and Nuna (USA)
for high‑end baby item flash sales. Posts every deal to the BotController
hub with ready‑to‑use affiliate tracking links.

If you supply your affiliate IDs in the config file, the bot will generate
commissionable links for each product.

Requirements:
    pip install requests beautifulsoup4 lxml

Configuration:
    A file named `baby_deals_config.json` is created on first run.
    Edit it to add your affiliate parameters and choose which brands to monitor.
"""

import json
import time
import requests
from pathlib import Path
from bs4 import BeautifulSoup

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "baby_gear_flash_sale"
BOT_NAME = "Baby Gear Flash Sale Affiliate"

# ── Configuration file ──────────────────────────────────────────────────────
CFG_FILE = Path(__file__).with_name("baby_deals_config.json")
STATE_FILE = Path(__file__).with_name("baby_deals_state.json")

DEFAULT_CONFIG = {
    "brands_to_watch": ["snoo", "nuna"],
    "snoo": {
        "sale_url": "https://www.happiestbaby.com/collections/snoo-sale",
        "affiliate_param": "ref",          # e.g., ?ref=YOUR_ID
        "affiliate_id": "my_snoo_affiliate"
    },
    "nuna": {
        "sale_url": "https://shop.nuna.eu/usa/collections/sale",  # US sale page
        "affiliate_param": None,           # Nuna doesn't publicly offer an affiliate link
        "affiliate_id": ""
    },
    "scan_interval_minutes": 15
}

# ── Hub helpers ─────────────────────────────────────────────────────────────
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

# ── Web fetching ────────────────────────────────────────────────────────────
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36")
}

def fetch_soup(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        r.raise_for_status()
        return BeautifulSoup(r.text, "lxml")
    except Exception:
        return None

# ── Deal extraction per brand ────────────────────────────────────────────────
def extract_snoo_deals(soup, config):
    """Return list of dicts with name, price, sale_price, link, affiliate_link."""
    if not soup:
        return []
    deals = []
    # Happiest Baby is on Shopify – product tiles often have class "grid-product__meta"
    # or "product-card". We'll try a few common selectors.
    product_cards = (
        soup.select(".product-card") or
        soup.select(".grid-product__meta") or
        soup.select('[data-product-card]') or
        soup.select('.product-item')
    )
    for card in product_cards:
        try:
            # Name
            name_elem = card.select_one(
                '.product-card__name, .grid-product__title, .product-item__title, .product-title'
            )
            name = name_elem.get_text(strip=True) if name_elem else "Unknown"

            # Link
            link_elem = card.find('a', href=True)
            if link_elem:
                link = link_elem['href']
                if not link.startswith('http'):
                    link = 'https://www.happiestbaby.com' + link
            else:
                continue

            # Prices – often a "compare at" and "price"
            compare_elem = card.select_one(
                '.product-card__compare-price, .price--compare, .product-item__price--compare, .compare-at'
            )
            price_elem = card.select_one(
                '.product-card__price, .price--sale, .product-item__price, .product-price'
            )
            compare = compare_elem.get_text(strip=True) if compare_elem else None
            price = price_elem.get_text(strip=True) if price_elem else None

            # Build affiliate link if config has param
            aff_param = config["snoo"].get("affiliate_param", "ref")
            aff_id = config["snoo"].get("affiliate_id", "")
            if aff_id and aff_param:
                separator = "&" if "?" in link else "?"
                aff_link = f"{link}{separator}{aff_param}={aff_id}"
            else:
                aff_link = link

            deals.append({
                "brand": "Snoo",
                "name": name,
                "original_price": compare,
                "sale_price": price,
                "url": link,
                "affiliate_link": aff_link
            })
        except Exception:
            continue
    return deals

def extract_nuna_deals(soup, config):
    """Return list of dicts for Nuna sale items."""
    if not soup:
        return []
    deals = []
    # Nuna EU/US store – likely Shopify as well. Product cards may be under .product-item
    product_cards = (
        soup.select('.product-item') or
        soup.select('.product-card') or
        soup.select('[data-product-card]') or
        soup.select('.grid-product__meta')
    )
    for card in product_cards:
        try:
            name_elem = card.select_one(
                '.product-item__title, .product-card__name, .grid-product__title, .product-title'
            )
            name = name_elem.get_text(strip=True) if name_elem else "Unknown"

            link_elem = card.find('a', href=True)
            if link_elem:
                link = link_elem['href']
                if not link.startswith('http'):
                    link = 'https://shop.nuna.eu' + link
            else:
                continue

            compare_elem = card.select_one(
                '.product-item__price--compare, .price--compare, .compare-at'
            )
            price_elem = card.select_one(
                '.product-item__price, .price--sale, .product-price'
            )
            compare = compare_elem.get_text(strip=True) if compare_elem else None
            price = price_elem.get_text(strip=True) if price_elem else None

            # Nuna does not officially offer affiliate links, leave as is
            aff_link = link  # placeholder – you can manually append your tracking if you have one

            deals.append({
                "brand": "Nuna",
                "name": name,
                "original_price": compare,
                "sale_price": price,
                "url": link,
                "affiliate_link": aff_link
            })
        except Exception:
            continue
    return deals

# ── State management ────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {"seen_urls": []}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

# ── Main scan ───────────────────────────────────────────────────────────────
def scan_all(config, state):
    """Run one pass over all configured brands, post new deals to hub."""
    if "snoo" in config.get("brands_to_watch", []):
        soup = fetch_soup(config["snoo"]["sale_url"])
        deals = extract_snoo_deals(soup, config)
        for d in deals:
            if d["url"] not in state["seen_urls"]:
                state["seen_urls"].append(d["url"])
                # Craft summary
                original = d["original_price"] or "?"
                sale = d["sale_price"] or "?"
                summary = (f"🛏️ Snoo Deal: {d['name']}  {original} → {sale}"
                           f"  🔗 {d['affiliate_link']}")
                post_to_hub(summary, "warning", d)
                time.sleep(0.5)  # rate-limit
        if not deals:
            post_to_hub("Snoo sale page returned no products – check selectors.", "warning")

    if "nuna" in config.get("brands_to_watch", []):
        soup = fetch_soup(config["nuna"]["sale_url"])
        deals = extract_nuna_deals(soup, config)
        for d in deals:
            if d["url"] not in state["seen_urls"]:
                state["seen_urls"].append(d["url"])
                original = d["original_price"] or "?"
                sale = d["sale_price"] or "?"
                summary = (f"🚼 Nuna Deal: {d['name']}  {original} → {sale}"
                           f"  🔗 {d['affiliate_link']}")
                post_to_hub(summary, "warning", d)
                time.sleep(0.5)
        if not deals:
            post_to_hub("Nuna sale page returned no products – check selectors.", "warning")

    save_state(state)

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, 'w') as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config file created at {CFG_FILE}. Edit it with your affiliate IDs, then restart.",
            "warning"
        )
        return

    with open(CFG_FILE, 'r') as f:
        config = json.load(f)

    post_to_hub(
        "Baby Gear Flash‑Sale Bot online – monitoring Snoo & Nuna for price drops.",
        "info"
    )

    state = load_state()
    interval = config.get("scan_interval_minutes", 15)

    while True:
        try:
            scan_all(config, state)
        except Exception as e:
            post_to_hub(f"Scan error: {e}", "error")
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
