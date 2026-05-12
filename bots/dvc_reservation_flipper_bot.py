#!/usr/bin/env python3
"""
dvc_reservation_flipper_bot.py — DVC Confirmed Reservation Flipper Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Scrapes DVC rental marketplaces (DVC Rental Store, David's, etc.)
   for confirmed reservations at sold‑out resorts.
2. Compares the listed price against a reference market value
   (scraped from other platforms or a predefined baseline).
3. Optionally auto‑buys the underpriced reservation via browser.
4. Auto‑relists the reservation on a different platform at a markup,
   capturing the spread.

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated scraping and purchasing may violate site Terms of Service.
  Use only with explicit permission.

SETUP
─────
1. Install dependencies:
      pip install playwright requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `dvc_flipper_config.json` (example at bottom).
   Fill in:
   - Your DVC rental site account credentials (if needed).
   - List of source marketplaces + CSS selectors.
   - Payment details for auto‑buying.
   - Proxy list.

5. Attach to BotController.
"""

import json
import os
import re
import time
import random
import threading
import requests
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from ebaysdk.trading import Connection as eBayTrading
    HAS_EBAY = True
except ImportError:
    HAS_EBAY = False

# ── BotController connection ─────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "dvc_reservation_flipper_bot"
BOT_NAME = "DVC Reservation Flipper"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dvc_flipper_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "dvc_flipper_state.json")

SCAN_INTERVAL      = 600   # 10 minutes between full scans
HEARTBEAT_INTERVAL = 30
_last_hb = 0.0
_last_hb_lock = threading.Lock()

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
    with _last_hb_lock:
        now = time.time()
        if now - _last_hb < HEARTBEAT_INTERVAL:
            return
        _last_hb = now
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Config & State ────────────────────────────────────────────────────────────
def load_config():
    default = {
        "sources": [
            {
                "name": "DVC Rental Store",
                "listings_url": "https://dvcrentalstore.com/confirmed-reservations/",
                "listing_selector": "div.reservation-item",
                "resort_selector": ".resort-name",
                "date_selector": ".dates",
                "price_selector": ".price",
                "detail_link_selector": "a.view-details",
                "checkout": {
                    "needs_login": True,
                    "email_selector": "#email",
                    "password_selector": "#password",
                    "login_submit": "button[type='submit']",
                    "buy_button_selector": "button.buy-now",
                    "fill_payment": True,
                    "payment_fields": {
                        "card_number": "#cardNumber",
                        "card_expiry": "#expiry",
                        "card_cvv": "#cvv",
                        "billing_zip": "#zip"
                    },
                    "place_order_selector": "button#placeOrder"
                }
            }
        ],
        "reference_market": {
            "method": "static",   # or "scrape_other_site"
            "static_markup": 2.0  # multiplier on listed price to define "below market"
        },
        "auto_buy": False,
        "auto_list": False,
        "relist_platform": {
            "type": "ebay",
            "markup_multiplier": 1.5,
            "listing_duration_days": 30
        },
        "payment": {
            "card_number": "4111111111111111",
            "card_expiry": "12/28",
            "card_cvv": "123",
            "name_on_card": "Your Name",
            "billing_zip": "12345"
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"}
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"flipped": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ── Proxy / Captcha (standard implementations) ─────────────────────────────
_proxy_list = CFG.get("proxies", {}).get("list", [])
_proxy_idx = 0
_proxy_lock = threading.Lock()

def next_proxy():
    if not _proxy_list:
        return None
    global _proxy_idx
    with _proxy_lock:
        p = _proxy_list[_proxy_idx % len(_proxy_list)]
        _proxy_idx += 1
        return p

CAPTCHA_KEY = CFG.get("captcha", {}).get("api_key", "").strip()

def solve_captcha(page, sitekey=None):
    if not CAPTCHA_KEY:
        return False
    try:
        if not sitekey:
            el = page.locator("[data-sitekey]")
            if el.count():
                sitekey = el.get_attribute("data-sitekey")
            else:
                return False
        resp = requests.get("http://2captcha.com/in.php", params={
            "key": CAPTCHA_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": page.url, "json": 1
        }, timeout=15).json()
        if resp.get("status") != 1:
            return False
        cid = resp["request"]
        for _ in range(30):
            time.sleep(5)
            r = requests.get("http://2captcha.com/res.php", params={
                "key": CAPTCHA_KEY, "action": "get", "id": cid, "json": 1
            }).json()
            if r.get("status") == 1:
                token = r["request"]
                page.evaluate(f"""
                    document.getElementById('g-recaptcha-response').innerHTML = '{token}';
                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                        Object.keys(___grecaptcha_cfg.clients).forEach(id =>
                            ___grecaptcha_cfg.clients[id].W.O.callback(token));
                    }}
                """)
                return True
            if r.get("request") != "CAPCHA_NOT_READY":
                break
    except:
        pass
    return False

# ── eBay relisting ─────────────────────────────────────────────────────────
def list_on_ebay(resort, dates, price):
    if not HAS_EBAY or not CFG.get("auto_list"):
        return
    trading = eBayTrading(
        domain="api.ebay.com",
        appid=os.getenv("EBAY_APP_ID"),
        certid=os.getenv("EBAY_CERT_ID"),
        devid=os.getenv("EBAY_DEV_ID"),
        token=os.getenv("EBAY_AUTH_TOKEN"),
        config_file=None
    )
    if not trading:
        return
    mark_up = CFG["relist_platform"]["markup_multiplier"]
    ebay_price = round(price * mark_up, 2)
    title = f"Disney DVC {resort} Confirmed Reservation {dates}"
    desc = f"Confirmed DVC reservation at {resort} for {dates}. Transferrable."
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": desc,
            "PrimaryCategory": {"CategoryID": "170067"},
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": f"Days_{CFG['relist_platform']['listing_duration_days']}",
            "Country": "US",
            "Currency": "USD",
            "ListingType": "FixedPriceItem",
            "Site": "US"
        }
    }
    try:
        resp = trading.execute("AddFixedPriceItem", payload)
        if resp.dict().get("Ack") == "Success":
            _post(f"eBay listing created: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay error: {e}", "error")

# ── Scrape listings ────────────────────────────────────────────────────────
def scrape_source(source_cfg):
    """Return list of reservation dicts from a source."""
    proxy = next_proxy()
    proxies = {"http": proxy, "https": proxy} if proxy else None
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    url = source_cfg["listings_url"]
    try:
        resp = requests.get(url, headers=headers, proxies=proxies, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        _post(f"Failed to scrape {source_cfg['name']}: {e}", "warning")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    items = soup.select(source_cfg["listing_selector"])
    results = []
    for item in items:
        resort = _extract_text(item, source_cfg["resort_selector"])
        dates = _extract_text(item, source_cfg["date_selector"])
        price = _extract_price(item, source_cfg["price_selector"])
        detail_link = _extract_link(item, source_cfg.get("detail_link_selector"), url)
        if resort and dates and price:
            results.append({
                "resort": resort,
                "dates": dates,
                "price": price,
                "url": detail_link,
                "source": source_cfg["name"]
            })
    return results

def _extract_text(soup, selector):
    el = soup.select_one(selector)
    return el.get_text(strip=True) if el else ""

def _extract_price(soup, selector):
    text = _extract_text(soup, selector)
    nums = re.findall(r'[\d,]+\.?\d{0,2}', text.replace(",", ""))
    return float(nums[0]) if nums else None

def _extract_link(soup, selector, base_url):
    el = soup.select_one(selector)
    if el and el.get("href"):
        return requests.compat.urljoin(base_url, el["href"])
    return None

# ── Determine if underpriced ───────────────────────────────────────────────
def is_underpriced(listing):
    method = CFG.get("reference_market", {}).get("method")
    if method == "static":
        # Compare price against a static multiple of the average? In this example,
        # we just define a fixed "market rate" per resort (hardcoded or configurable)
        market_rates = {
            "Grand Floridian": 25.0,  # per point example
            "Polynesian Bungalow": 35.0,
            "Bay Lake Tower": 20.0,
        }
        base = market_rates.get(listing["resort"], 20.0) * 100  # approximate
        return listing["price"] < base * 0.8  # 20% below
    # Could implement cross‑site comparison here
    return False

# ── Auto‑buy via browser ──────────────────────────────────────────────────
def auto_buy_reservation(source_cfg, listing):
    if not HAS_PLAYWRIGHT or not CFG.get("auto_buy"):
        return False
    checkout = source_cfg.get("checkout", {})
    payment = CFG["payment"]
    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            # Go to detail page
            _post(f"Navigating to {listing['url']}...", "info")
            page.goto(listing["url"], wait_until="networkidle", timeout=30000)
            # Login if needed
            if checkout.get("needs_login"):
                page.fill(checkout["email_selector"], "your@email.com")  # config could hold account
                page.fill(checkout["password_selector"], "yourpass")
                page.click(checkout["login_submit"])
                page.wait_for_load_state("networkidle")
            # Click "Buy Now"
            page.click(checkout["buy_button_selector"])
            page.wait_for_load_state("networkidle")
            # Fill payment
            page.fill(checkout["payment_fields"]["card_number"], payment["card_number"])
            page.fill(checkout["payment_fields"]["card_expiry"], payment["card_expiry"])
            page.fill(checkout["payment_fields"]["card_cvv"], payment["card_cvv"])
            page.fill(checkout["payment_fields"]["billing_zip"], payment["billing_zip"])
            # Captcha
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    return False
            # Place order
            page.click(checkout["place_order_selector"])
            page.wait_for_timeout(8000)
            if "thank you" in page.content().lower() or "confirmation" in page.content().lower():
                _post(f"Purchased reservation at {listing['resort']} for ${listing['price']}", "info")
                return True
            return False
        except Exception as e:
            _post(f"Auto-buy error: {e}", "error")
            return False
        finally:
            browser.close()

# ── Main scan loop ────────────────────────────────────────────────────────
def scan():
    sources = CFG.get("sources", [])
    for src in sources:
        listings = scrape_source(src)
        _post(f"Scraped {len(listings)} listings from {src['name']}.", "info")
        for listing in listings:
            # Skip already processed (by URL)
            if listing["url"] in [f.get("url") for f in STATE.get("flipped", [])]:
                continue
            if is_underpriced(listing):
                _post(f"Underpriced! {listing['resort']} {listing['dates']} at ${listing['price']}", "warning")
                bought = auto_buy_reservation(src, listing)
                if bought or not CFG.get("auto_buy"):
                    # Record flip (mark as processed)
                    STATE.setdefault("flipped", []).append({
                        "url": listing["url"],
                        "resort": listing["resort"],
                        "price": listing["price"],
                        "timestamp": datetime.utcnow().isoformat(),
                        "bought": bought
                    })
                    save_state(STATE)
                    # Relist on eBay (even if we didn't auto-buy, but we found it underpriced)
                    if CFG.get("auto_list"):
                        list_on_ebay(listing["resort"], listing["dates"], listing["price"] * 1.5)
                # Avoid hammering
                time.sleep(random.uniform(2, 5))

def main():
    _wait_for_hub()
    _post("DVC Reservation Flipper Bot online.", "info")
    while True:
        scan()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `dvc_flipper_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "sources": [
    {
      "name": "DVC Rental Store",
      "listings_url": "https://dvcrentalstore.com/confirmed-reservations/",
      "listing_selector": "div.reservation-item",
      "resort_selector": ".resort-name",
      "date_selector": ".dates",
      "price_selector": ".price",
      "detail_link_selector": "a.view-details",
      "checkout": {
        "needs_login": true,
        "email_selector": "#email",
        "password_selector": "#password",
        "login_submit": "button[type='submit']",
        "buy_button_selector": "button.buy-now",
        "fill_payment": true,
        "payment_fields": {
          "card_number": "#cardNumber",
          "card_expiry": "#expiry",
          "card_cvv": "#cvv",
          "billing_zip": "#zip"
        },
        "place_order_selector": "button#placeOrder"
      }
    }
  ],
  "reference_market": {
    "method": "static",
    "static_markup": 2.0
  },
  "auto_buy": false,
  "auto_list": false,
  "relist_platform": {
    "type": "ebay",
    "markup_multiplier": 1.5,
    "listing_duration_days": 30
  },
  "payment": {
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "name_on_card": "Your Name",
    "billing_zip": "12345"
  },
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"}
}
"""
