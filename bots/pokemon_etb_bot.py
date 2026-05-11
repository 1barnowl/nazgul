#!/usr/bin/env python3
"""
pokemon_etb_bot.py — Elite Trainer Box Auto‑Buy & Resell Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors retailers for ETB stock.
2. Automates checkout with Playwright + proxy rotation + 2Captcha.
3. Lists purchased ETBs on TCGPlayer at a markup.

SETUP
─────
1. Install dependencies:
      pip install requests playwright beautifulsoup4
      python -m playwright install chromium

   For TCGPlayer listing:
      pip install tcgplayer-api-wrapper   (or raw requests)

2. Get a 2Captcha API key (https://2captcha.com/) and export:
      export CAPTCHA_API_KEY="your-key"

3. Get TCGPlayer seller API credentials (Personal Access Token):
      export TCGPLAYER_ACCESS_TOKEN="your-token"

4. Create `etb_config.json` (example at bottom). Provide:
   - Product URLs and CSS selectors for each retailer.
   - Checkout details (with payment info).
   - Proxy list (optional).

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
from urllib.parse import urljoin
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "pokemon_etb_bot"
BOT_NAME = "Pokemon ETB Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "etb_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "etb_state.json")

SCAN_INTERVAL      = 30    # seconds between checks
HEARTBEAT_INTERVAL = 20
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

# ── Config ─────────────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "etbs": [
                {
                    "name": "Pokémon TCG: Scarlet & Violet—151 Elite Trainer Box",
                    "retailers": [
                        {
                            "site": "Pokemon Center",
                            "url": "https://www.pokemoncenter.com/product/699-15360/",
                            "stock_selector": "button.add-to-cart",
                            "sold_out_selector": "span.sold-out-text",
                            "price_selector": "span.price",
                            "add_to_cart_selector": "button.add-to-cart",
                            "max_qty": 2
                        },
                        {
                            "site": "Target",
                            "url": "https://www.target.com/p/pokemon-scarlet-violet-151-elite-trainer-box/-/A-89612679",
                            "stock_selector": "button[data-test='addToCartButton']",
                            "sold_out_selector": "button[data-test='soldOutButton']",
                            "price_selector": "span[data-test='product-price']",
                            "add_to_cart_selector": "button[data-test='addToCartButton']",
                            "max_qty": 3
                        }
                    ],
                    "default_purchase_price_usd": 49.99,
                    "tcgplayer_product_id": 501234  # obtain from TCGPlayer catalog
                }
            ],
            "checkout_profile": {
                "email": "ash@example.com",
                "first_name": "Ash",
                "last_name": "Ketchum",
                "address": "123 Pallet St",
                "city": "Viridian",
                "state": "CA",
                "zip": "90001",
                "card_number": "4111111111111111",
                "card_expiry": "12/26",
                "card_cvv": "123"
            },
            "proxies": {
                "list": []
            },
            "captcha": {
                "api_key": os.getenv("CAPTCHA_API_KEY", ""),
                "service": "2captcha"
            },
            "tcgplayer": {
                "access_token": os.getenv("TCGPLAYER_ACCESS_TOKEN", ""),
                "markup_multiplier": 2.5,
                "auto_list": True,
                "condition": "Near Mint",
                "quantity": 1
            }
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── State ──────────────────────────────────────────────────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"purchased": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Proxy rotation ─────────────────────────────────────────────────────────────
_proxy_list = CFG.get("proxies", {}).get("list", [])
_proxy_index = 0
_proxy_lock = threading.Lock()

def next_proxy():
    if not _proxy_list:
        return None
    global _proxy_index
    with _proxy_lock:
        proxy = _proxy_list[_proxy_index % len(_proxy_list)]
        _proxy_index += 1
        return proxy

# ── CAPTCHA solving (2Captcha) ─────────────────────────────────────────────────
CAPTCHA_API_KEY = CFG.get("captcha", {}).get("api_key", "").strip()

def solve_captcha(page, sitekey=None):
    if not CAPTCHA_API_KEY:
        _post("No 2Captcha API key.", "error")
        return False
    try:
        if not sitekey:
            sitekey_elem = page.locator("[data-sitekey]")
            if sitekey_elem.count():
                sitekey = sitekey_elem.get_attribute("data-sitekey")
            else:
                _post("No sitekey found.", "error")
                return False
        url = page.url
        resp = requests.get("http://2captcha.com/in.php", params={
            "key": CAPTCHA_API_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": url, "json": 1
        }, timeout=15).json()
        if resp.get("status") != 1:
            _post(f"2Captcha submission failed: {resp.get('request')}", "error")
            return False
        captcha_id = resp["request"]
        _post(f"Solving CAPTCHA {captcha_id}...", "info")
        for _ in range(30):
            time.sleep(5)
            result = requests.get("http://2captcha.com/res.php", params={
                "key": CAPTCHA_API_KEY, "action": "get",
                "id": captcha_id, "json": 1
            }).json()
            if result.get("status") == 1:
                token = result["request"]
                page.evaluate(f"""
                    document.getElementById('g-recaptcha-response').innerHTML = '{token}';
                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                        Object.keys(___grecaptcha_cfg.clients).forEach(id =>
                            ___grecaptcha_cfg.clients[id].W.O.callback(token));
                    }}
                """)
                _post("CAPTCHA solved!", "info")
                return True
            if result.get("request") != "CAPCHA_NOT_READY":
                break
        _post("CAPTCHA solving timed out.", "error")
        return False
    except Exception as e:
        _post(f"CAPTCHA error: {e}", "error")
        return False

# ── Checkout automation ────────────────────────────────────────────────────────
def perform_checkout(retailer_cfg, qty=1):
    """
    Launches Playwright browser, adds ETB to cart, fills checkout
    details from global profile, solves captcha, and places order.
    Returns True if success.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return False

    profile = CFG.get("checkout_profile", {})
    proxy = next_proxy()
    launch_options = {"headless": False}
    if proxy:
        launch_options["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_options)
        page = browser.new_page()
        try:
            # 1. Navigate to product page
            _post(f"Navigating to {retailer_cfg['url']}", "info")
            page.goto(retailer_cfg["url"], wait_until="networkidle", timeout=30000)

            # 2. Check stock (redundant, but safety)
            add_sel = retailer_cfg["add_to_cart_selector"]
            if not page.locator(add_sel).is_visible():
                _post("Add to Cart button not visible — maybe out of stock.", "warning")
                return False

            # 3. Adjust quantity if needed
            if qty > 1:
                qty_input = page.locator("input[type='number'], select.qty")
                if qty_input.count():
                    qty_input.fill(str(qty))
                else:
                    # Click "Add to Cart" multiple times? Better to set quantity if possible.
                    pass
            page.click(add_sel)
            _post("Added to cart.", "info")

            # 4. Go to cart / checkout
            page.goto("https://www.pokemoncenter.com/cart", wait_until="networkidle", timeout=30000)
            if page.locator("text=Checkout").count():
                page.click("text=Checkout")
            elif page.locator("a:has-text('Checkout')").count():
                page.click("a:has-text('Checkout')")
            elif retailer_cfg.get("checkout_url"):
                page.goto(retailer_cfg["checkout_url"], wait_until="networkidle", timeout=30000)
            else:
                _post("Cannot find checkout link.", "error")
                return False

            # 5. Fill checkout form using generic selectors
            _fill_checkout_form(page, profile)

            # 6. CAPTCHA
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                _post("CAPTCHA detected.", "info")
                if not solve_captcha(page):
                    return False
                time.sleep(2)

            # 7. Place order
            place_btn = "button:has-text('Place Order'), button#placeOrder, button[type='submit']"
            page.click(place_btn)
            page.wait_for_timeout(5000)

            if "thank you" in page.content().lower() or "order confirmed" in page.content().lower():
                _post("Order placed successfully!", "info")
                return True
            else:
                _post("Order might not have succeeded – check manually.", "warning")
                return False
        except Exception as e:
            _post(f"Checkout error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_checkout_form(page, profile):
    """Fill common checkout fields; adapt per retailer as needed."""
    field_mapping = {
        "input#email":         profile.get("email"),
        "input#firstName":     profile.get("first_name"),
        "input#lastName":      profile.get("last_name"),
        "input#address1":      profile.get("address"),
        "input#city":          profile.get("city"),
        "select#state":        profile.get("state"),
        "input#postalCode":    profile.get("zip"),
        "input#cardNumber":    profile.get("card_number"),
        "input#cardExpiry":    profile.get("card_expiry"),
        "input#cardCvv":       profile.get("card_cvv"),
    }
    for selector, value in field_mapping.items():
        if value:
            try:
                if selector.startswith("select"):
                    page.select_option(selector, value)
                else:
                    page.fill(selector, value)
            except Exception:
                pass

# ── TCGPlayer listing ──────────────────────────────────────────────────────────
TCG_TOKEN = CFG.get("tcgplayer", {}).get("access_token", "").strip()

def list_on_tcgplayer(product_id, purchase_price, name):
    """Create a fixed‑price listing on TCGPlayer via their API."""
    if not TCG_TOKEN:
        _post("TCGPlayer access token missing. Cannot list.", "warning")
        return None

    markup = CFG["tcgplayer"]["markup_multiplier"]
    price = round(purchase_price * markup, 2)

    headers = {
        "Authorization": f"Bearer {TCG_TOKEN}",
        "Content-Type": "application/json"
    }

    body = {
        "productConditionId": 1,  # 1 = Near Mint (assuming)
        "price": {
            "currencyCode": "USD",
            "value": price
        },
        "quantity": CFG["tcgplayer"].get("quantity", 1),
        "channelId": 0  # 0 = marketplace
    }

    try:
        resp = requests.post(
            f"https://api.tcgplayer.com/inventory/products/{product_id}/listings",
            headers=headers,
            json=body,
            timeout=15
        )
        if resp.status_code == 200 or resp.status_code == 201:
            _post(f"Listed {name} on TCGPlayer at ${price}", "info")
            return True
        else:
            _post(f"TCGPlayer listing failed: {resp.status_code} {resp.text[:200]}", "error")
            return False
    except Exception as e:
        _post(f"TCGPlayer API error: {e}", "error")
        return False

# ── Scanning loop ──────────────────────────────────────────────────────────────
def scan_etbs():
    state = load_state()
    etbs = CFG.get("etbs", [])

    for etb in etbs:
        for retailer in etb.get("retailers", []):
            site = retailer["site"]
            url = retailer["url"]
            stock_sel = retailer["stock_selector"]
            sold_out_sel = retailer.get("sold_out_selector", "")

            # Quick HTTP pre-check
            proxy = next_proxy()
            proxies = {"http": proxy, "https": proxy} if proxy else None
            try:
                resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15, proxies=proxies)
                soup = BeautifulSoup(resp.text, "html.parser")
                in_stock = bool(soup.select_one(stock_sel))
                if sold_out_sel and soup.select_one(sold_out_sel):
                    in_stock = False
            except Exception as e:
                _post(f"{site} pre-check error: {e}", "warning")
                continue

            if not in_stock:
                _post(f"{site} – {etb['name']} out of stock.", "info")
                continue

            # Prevent duplicate purchases within 24h for same URL+site
            already = any(
                p["url"] == url and p["site"] == site and
                (datetime.utcnow() - datetime.fromisoformat(p["timestamp"])) < timedelta(hours=24)
                for p in state.get("purchased", [])
            )
            if already:
                _post(f"{site} – already purchased recently.", "info")
                continue

            _post(f"{site} – IN STOCK! Initiating checkout for {etb['name']}...", "error")
            qty = retailer.get("max_qty", 1)
            success = perform_checkout(retailer, qty)
            if success:
                # Record purchase
                state.setdefault("purchased", []).append({
                    "url": url,
                    "site": site,
                    "product": etb["name"],
                    "timestamp": datetime.utcnow().isoformat(),
                    "price_paid": retailer.get("default_purchase_price_usd", etb.get("default_purchase_price_usd", 50))
                })
                save_state(state)

                # Auto-list on TCGPlayer
                if CFG["tcgplayer"].get("auto_list"):
                    tcg_id = etb.get("tcgplayer_product_id")
                    if tcg_id:
                        list_on_tcgplayer(
                            tcg_id,
                            retailer.get("default_purchase_price_usd", 50),
                            etb["name"]
                        )
                    else:
                        _post(f"No TCGPlayer product ID for {etb['name']} – skip listing.", "warning")
            # Delay between retail attempts
            time.sleep(random.uniform(10, 30))

def main():
    _wait_for_hub()
    _post("Pokémon ETB Bot online. Scanning retailers for Elite Trainer Boxes...", "info")
    while True:
        scan_etbs()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `etb_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "etbs": [
    {
      "name": "Scarlet & Violet—151 Elite Trainer Box",
      "default_purchase_price_usd": 49.99,
      "tcgplayer_product_id": 501234,
      "retailers": [
        {
          "site": "Pokemon Center",
          "url": "https://www.pokemoncenter.com/product/699-15360/",
          "stock_selector": "button.add-to-cart",
          "sold_out_selector": "span.sold-out-text",
          "price_selector": "span.price",
          "add_to_cart_selector": "button.add-to-cart",
          "max_qty": 2
        },
        {
          "site": "Target",
          "url": "https://www.target.com/p/pokemon-scarlet-violet-151-elite-trainer-box/-/A-89612679",
          "stock_selector": "button[data-test='addToCartButton']",
          "sold_out_selector": "button[data-test='soldOutButton']",
          "price_selector": "span[data-test='product-price']",
          "add_to_cart_selector": "button[data-test='addToCartButton']",
          "max_qty": 3
        }
      ]
    }
  ],
  "checkout_profile": {
    "email": "you@domain.com",
    "first_name": "Ash",
    "last_name": "Ketchum",
    "address": "123 Pallet St",
    "city": "Viridian",
    "state": "CA",
    "zip": "90001",
    "card_number": "4111111111111111",
    "card_expiry": "12/26",
    "card_cvv": "123"
  },
  "proxies": {
    "list": []
  },
  "captcha": {
    "api_key": "YOUR_2CAPTCHA_KEY"
  },
  "tcgplayer": {
    "access_token": "YOUR_TGC_ACCESS_TOKEN",
    "markup_multiplier": 2.5,
    "auto_list": true,
    "quantity": 1
  }
}
"""
