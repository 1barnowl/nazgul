#!/usr/bin/env python3
"""
rent_the_runway_arbitrage_bot.py — Designer Rental Arbitrage Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scrapes Rent the Runway’s clearance sale for heavily discounted designer
dresses, then calculates a profitable resale price for Poshmark. The bot
posts each “arbitrage opportunity” to the BotController hub so you can
quickly list it. For a true dropship flow, you’d wait for a Poshmark sale
and then purchase from RTR (see instructions at the bottom).

Real data – no simulation.

Requirements:
    pip install requests beautifulsoup4

Configuration:
    A file named `rtr_arbitrage_config.json` will be created on first run.
    Edit it to set your profit multiplier and scan interval.
"""

import json
import time
import requests
from pathlib import Path
from bs4 import BeautifulSoup

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "rtr_arbitrage"
BOT_NAME = "Rent the Runway Arbitrage"

# ── Config file ─────────────────────────────────────────────────────────────
CFG_FILE = Path(__file__).with_name("rtr_arbitrage_config.json")
STATE_FILE = Path(__file__).with_name("rtr_arbitrage_state.json")

DEFAULT_CONFIG = {
    "rtr_sale_url": "https://www.renttherunway.com/sale/all",
    "profit_multiplier": 2.5,      # resale price = RTR clearance price * multiplier
    "max_resale_price": 600,       # cap to stay competitive on Poshmark
    "scan_interval_minutes": 30
}

# ── Hub posting ─────────────────────────────────────────────────────────────
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

# ── RTR Clearance scraping ──────────────────────────────────────────────────
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                  " (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
}

def fetch_sale_page(url):
    """Return the page HTML (str) or None."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        return r.text
    except Exception as e:
        post_to_hub(f"Failed to fetch RTR sale page: {e}", "error")
        return None

def extract_products_from_html(html):
    """
    Extract product data from RTR's Next.js __NEXT_DATA__ JSON blob.
    Returns list of dicts with keys: id, name, designer, original_price,
    clearance_price, sizes, url, image_url.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "lxml")
    script_tag = soup.find("script", id="__NEXT_DATA__")
    if not script_tag:
        # fallback: maybe classic HTML? but unlikely
        post_to_hub("No __NEXT_DATA__ found on RTR page – structure may have changed.", "warning")
        return []
    try:
        data = json.loads(script_tag.string)
        # Navigate to product listing in Next.js props
        # Usually: data["props"]["pageProps"]["initialData"]["listings"] or similar
        # We'll do a safe navigation
        listings = (
            data.get("props", {})
                .get("pageProps", {})
                .get("initialData", {})
                .get("listings", [])
        )
        if not listings:
            # Try alternative paths
            listings = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("products", [])
            )
        if not listings:
            # Maybe it's in a different shape
            # Search for any list of products
            for key, value in data.items():
                if isinstance(value, list):
                    # check if first item has 'id' and 'name'
                    if value and isinstance(value[0], dict) and 'id' in value[0] and 'name' in value[0]:
                        listings = value
                        break
            if not listings:
                # Another attempt: in props.pageProps.dehydratedState.queries[...].state.data.listings
                dehydrated = (
                    data.get("props", {})
                        .get("pageProps", {})
                        .get("dehydratedState", {})
                        .get("queries", [])
                )
                for query in dehydrated:
                    state = query.get("state", {}).get("data", {})
                    if "listings" in state:
                        listings = state["listings"]
                        break
        if not listings:
            post_to_hub("Could not locate product listings in RTR page data.", "warning")
            return []

        products = []
        for item in listings:
            try:
                pid = item.get("id")
                name = item.get("name", "Unknown")
                designer = item.get("designer", {}).get("name", "Unknown")
                # Pricing: may be "salePrice" or "price"
                orig = item.get("retailPrice") or item.get("originalPrice") or item.get("original_price")
                sale = item.get("salePrice") or item.get("price") or item.get("currentPrice")
                # Convert to float if possible
                orig = float(orig) if orig is not None else None
                sale = float(sale) if sale is not None else None
                # Sizes
                sizes = [s.get("sizeLabel") or s.get("name") for s in item.get("sizes", [])]
                # URL
                slug = item.get("slug") or item.get("url")
                product_url = f"https://www.renttherunway.com/shop/{slug}" if slug else None
                # Images
                images = item.get("images", [])
                image_url = images[0].get("url") if images else None

                if not pid or not sale:
                    continue  # skip incomplete

                products.append({
                    "id": pid,
                    "name": name,
                    "designer": designer,
                    "original_price": orig,
                    "clearance_price": sale,
                    "available_sizes": sizes,
                    "url": product_url,
                    "image_url": image_url,
                })
            except Exception:
                continue
        return products
    except Exception as e:
        post_to_hub(f"Error parsing RTR data: {e}", "error")
        return []

# ── State management ────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_product_ids": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Main scan + alert ───────────────────────────────────────────────────────
def scan(config, state):
    """Fetch RTR clearance, find new products, compute resale price, post to hub."""
    products = extract_products_from_html(fetch_sale_page(config["rtr_sale_url"]))
    if not products:
        return

    multiplier = config.get("profit_multiplier", 2.5)
    max_resale = config.get("max_resale_price", 600)

    new_count = 0
    for p in products:
        if p["id"] in state["seen_product_ids"]:
            continue
        state["seen_product_ids"].append(p["id"])
        new_count += 1

        clearance = p["clearance_price"]
        # Skip if no clearance price
        if clearance is None:
            continue
        # Calculate resale price
        resale = min(clearance * multiplier, max_resale)
        profit = resale - clearance
        if profit <= 0:
            continue  # no profit

        # Construct summary and payload
        sizes = ", ".join(p["available_sizes"]) if p["available_sizes"] else "N/A"
        summary = (
            f"🛍️ ARBITRAGE: {p['designer']} – {p['name']} "
            f"(Clearance ${clearance:.2f} → Sell ${resale:.2f}) "
            f"Profit ~${profit:.2f}"
        )
        payload = {
            "designer": p["designer"],
            "name": p["name"],
            "rtr_url": p["url"],
            "clearance_price": clearance,
            "suggested_resale_price": resale,
            "estimated_profit": profit,
            "sizes": p["available_sizes"],
            "image_url": p["image_url"],
            # Ready-to-list Poshmark description
            "poshmark_description": (
                f"🆕 {p['designer']} {p['name']}\n"
                f"Retail ${p['original_price'] if p['original_price'] else 'N/A'}\n"
                f"New without tags, straight from Rent the Runway clearance.\n"
                f"Available sizes: {sizes}\n\n"
                f"Message me with any questions!"
            ),
            "poshmark_category": "Women > Dresses"  # can be refined
        }
        post_to_hub(summary, "warning", payload)
        time.sleep(0.5)  # rate limiter

    if new_count > 0:
        post_to_hub(f"✅ {new_count} new arbitrage opportunities posted.", "info", {"total": new_count})
    else:
        post_to_hub("🔍 No new clearance deals found.", "info")

    save_state(state)

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config file created at {CFG_FILE}. Adjust profit settings, then restart.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    post_to_hub(
        "RTR Arbitrage Bot online – scanning clearance for resale opportunities.",
        "info"
    )

    state = load_state()
    interval = config.get("scan_interval_minutes", 30)

    while True:
        try:
            scan(config, state)
        except Exception as e:
            post_to_hub(f"Scan error: {e}", "error")
        time.sleep(interval * 60)

if __name__ == "__main__":
    main()
