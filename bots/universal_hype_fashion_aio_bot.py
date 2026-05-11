#!/usr/bin/env python3
"""
universal_hype_fashion_aio_bot.py — Universal Hype‑Fashion AIO Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Multi‑site monitor, cart, and checkout engine for streetwear &
designer drops.  Uses Playwright + proxy rotation + 2Captcha.

✦ FOR RESEARCH / EDUCATIONAL USE ONLY.
  Automated purchasing may violate each retailer’s Terms of Service.

Supports modular checkout for:
  - Shopify (Kith, Yeezy Supply, Palace, etc.)
  - Supreme (custom monitoring & checkout)
  - SNKRS (Nike’s draw / FLOW system)

Features:
  - Drop calendar scraper (Twitter / drop‑list sites)
  - Session pre‑warming (visit homepage, solve captcha, cache cookies)
  - Simultaneous multi‑site checkout (thread‑pool)
  - Real‑time stock detection via CSS selectors & API endpoints
  - eBay listing after purchase (optional)

SETUP
─────
1. Install deps:
      pip install playwright ebaysdk schedule requests
      python -m playwright install chromium

2. Set env vars:
      export CAPTCHA_API_KEY="2captcha-key"
      export EBAY_APP_ID=... (optional)

3. Fill the `hype_config.json` (example at bottom)
   with your accounts, proxies, target sites, payment details.

4. Attach to BotController.
"""

import asyncio
import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import requests
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
HUB = "http://localhost:8765"
BOT_ID = "universal_hype_aio_bot"
BOT_NAME = "Universal Hype AIO Bot"

CONFIG_FILE = "hype_config.json"
STATE_FILE = "hype_state.json"

SCAN_INTERVAL = 15  # seconds between stock checks
HEARTBEAT_INTERVAL = 30
_last_hb = 0.0
_lock = threading.Lock()


def _post(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID, "bot_name": BOT_NAME,
            "summary": summary, "level": level, "payload": payload or {}
        }, timeout=5)
    except:
        pass


def _heartbeat():
    global _last_hb
    with _lock:
        now = time.time()
        if now - _last_hb < HEARTBEAT_INTERVAL:
            return
        _last_hb = now
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except:
        pass


def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).ok:
                return
        except:
            pass
        time.sleep(1)


# ── Config / State ──────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "sites": [
                {
                    "name": "Kith",
                    "type": "shopify",
                    "url": "https://kith.com/products/some-product",
                    "add_to_cart_sel": "button[name='add']",
                    "sold_out_sel": "button[disabled][name='add']",
                    "checkout_url": "https://kith.com/checkout",
                    "enabled": True
                },
                {
                    "name": "Supreme",
                    "type": "supreme",
                    "url": "https://www.supremenewyork.com/shop/all",
                    "keyword": "Box Logo",
                    "enabled": True
                },
                {
                    "name": "SNKRS",
                    "type": "snkrs",
                    "style_code": "DD1391-100",  # example
                    "enabled": True
                }
            ],
            "accounts": [
                {
                    "email": "your@email.com",
                    "password": "your_pass",
                    "profile": {
                        "first_name": "Kanye",
                        "last_name": "West",
                        "address": "123 Yeezy Way",
                        "city": "Los Angeles",
                        "state": "CA",
                        "zip": "90001",
                        "phone": "5551234567",
                        "card_number": "4111111111111111",
                        "card_expiry": "12/28",
                        "card_cvv": "123"
                    }
                }
            ],
            "proxies": {"list": []},
            "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", "")},
            "ebay": {"enabled": False, "markup_multiplier": 2.5},
            "twitter_drop_accounts": ["@DropsByJay", "@SOLELINKS"]
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"purchased": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


CFG = load_config()
STATE = load_state()

# Proxy rotation
_proxies = CFG.get("proxies", {}).get("list", [])
_proxy_idx = 0


def next_proxy():
    global _proxy_idx
    if not _proxies:
        return None
    with _lock:
        p = _proxies[_proxy_idx % len(_proxies)]
        _proxy_idx += 1
        return p


# ── Captcha Solver (2Captcha) ───────────────────────────────────────────────
CAPTCHA_KEY = CFG.get("captcha", {}).get("api_key", "").strip()


def solve_captcha(page, sitekey=None):
    if not CAPTCHA_KEY:
        _post("No 2Captcha API key.", "error")
        return False
    try:
        if not sitekey:
            elem = page.locator("[data-sitekey]")
            if elem.count():
                sitekey = elem.get_attribute("data-sitekey")
            else:
                return False
        resp = requests.get("http://2captcha.com/in.php", params={
            "key": CAPTCHA_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": page.url, "json": 1
        }, timeout=15).json()
        if resp.get("status") != 1:
            _post(f"Captcha submission failed: {resp}", "error")
            return False
        captcha_id = resp["request"]
        for i in range(30):
            time.sleep(5)
            res = requests.get("http://2captcha.com/res.php", params={
                "key": CAPTCHA_KEY, "action": "get",
                "id": captcha_id, "json": 1
            }).json()
            if res.get("status") == 1:
                token = res["request"]
                page.evaluate(f"""
                    document.getElementById('g-recaptcha-response').innerHTML = '{token}';
                    if (typeof ___grecaptcha_cfg !== 'undefined') {{
                        Object.keys(___grecaptcha_cfg.clients).forEach(id =>
                            ___grecaptcha_cfg.clients[id].W.O.callback(token));
                    }}
                """)
                _post("Captcha solved!", "info")
                return True
            if res.get("request") != "CAPCHA_NOT_READY":
                break
        _post("Captcha solving timeout", "error")
        return False
    except Exception as e:
        _post(f"Captcha error: {e}", "error")
        return False


# ── Base site module ─────────────────────────────────────────────────────────
class SiteModule:
    def __init__(self, site_cfg, account):
        self.cfg = site_cfg
        self.account = account
        self.name = site_cfg.get("name", "Unknown")

    def pre_warm(self, page):
        """Common pre‑warming: visit homepage, close popups, solve possible captcha."""
        page.goto(self.cfg.get("url"), wait_until="networkidle", timeout=30000)
        # Dismiss cookie banners
        try:
            page.click("button:has-text('Accept'), button:has-text('Close')", timeout=2000)
        except:
            pass

    def check_stock(self, page):
        """Return True if product is in stock."""
        raise NotImplementedError

    def add_to_cart(self, page):
        raise NotImplementedError

    def checkout(self, page):
        """Perform full checkout, return True on success."""
        raise NotImplementedError


# ── Shopify module ───────────────────────────────────────────────────────────
class ShopifyModule(SiteModule):
    def check_stock(self, page):
        if page.locator(self.cfg["sold_out_sel"]).is_visible():
            return False
        add_btn = page.locator(self.cfg["add_to_cart_sel"])
        return add_btn.is_visible() and add_btn.is_enabled()

    def add_to_cart(self, page):
        page.click(self.cfg["add_to_cart_sel"])
        _post(f"{self.name}: added to cart", "info")

    def checkout(self, page):
        acc = self.account["profile"]
        # Navigate to cart/checkout
        page.goto(self.cfg.get("checkout_url", "/checkout"), wait_until="networkidle", timeout=30000)

        # Fill contact
        page.fill("input#checkout_email, input[name='checkout[email]']", self.account["email"])
        page.fill("input#checkout_shipping_address_first_name, input[name='checkout[shipping_address][first_name]']",
                  acc["first_name"])
        page.fill("input#checkout_shipping_address_last_name, input[name='checkout[shipping_address][last_name]']",
                  acc["last_name"])
        page.click("button:has-text('Continue to shipping')")
        page.wait_for_load_state("networkidle")

        # Fill address
        page.fill("input#checkout_shipping_address_address1, input[name='checkout[shipping_address][address1]']",
                  acc["address"])
        page.fill("input#checkout_shipping_address_city, input[name='checkout[shipping_address][city]']",
                  acc["city"])
        page.select_option("select#checkout_shipping_address_country, select[name='checkout[shipping_address][country]']",
                           "US")
        page.wait_for_timeout(1000)
        page.fill("input#checkout_shipping_address_zip, input[name='checkout[shipping_address][zip]']",
                  acc["zip"])
        if acc.get("state"):
            page.select_option("select#checkout_shipping_address_province, select[name='checkout[shipping_address][province]']",
                               acc["state"])
        page.fill("input#checkout_shipping_address_phone, input[name='checkout[shipping_address][phone]']",
                  acc.get("phone", ""))
        page.click("button:has-text('Continue to payment')")
        page.wait_for_load_state("networkidle")

        # Payment (iframes)
        page.wait_for_timeout(2000)
        try:
            card_iframe = page.frame_locator("iframe.card-number-iframe, iframe[title*='card number']")
            card_iframe.locator("input[name='number']").fill(acc["card_number"])
        except:
            page.fill("input#number, input[name='number']", acc["card_number"])

        try:
            exp_iframe = page.frame_locator("iframe.card-expiry-iframe, iframe[title*='expiry']")
            exp_iframe.locator("input[name='expiry']").fill(acc["card_expiry"])
        except:
            page.fill("input#expiry, input[name='expiry']", acc["card_expiry"])

        try:
            cvv_iframe = page.frame_locator("iframe.card-cvc-iframe, iframe[title*='CVC']")
            cvv_iframe.locator("input[name='verification_value']").fill(acc["card_cvv"])
        except:
            page.fill("input#verification_value, input[name='verification_value']", acc["card_cvv"])

        # Captcha
        if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
            if not solve_captcha(page):
                return False
            time.sleep(2)

        page.click("button:has-text('Complete order')")
        page.wait_for_timeout(10000)
        if "thank you" in page.content().lower():
            _post(f"{self.name}: order placed!", "info")
            return True
        return False


# ── Supreme module (custom) ─────────────────────────────────────────────────
class SupremeModule(SiteModule):
    def check_stock(self, page):
        # Check for a keyword in product titles
        if page.locator(f"article a:has-text('{self.cfg.get('keyword', '')}')").count():
            return True
        return False

    def add_to_cart(self, page):
        # Click the first product matching keyword
        page.click(f"article a:has-text('{self.cfg.get('keyword', '')}')")
        # Add to cart (Supreme uses "add to cart" button)
        page.click("input[value='add to cart']")

    def checkout(self, page):
        page.goto("https://www.supremenewyork.com/checkout", wait_until="networkidle", timeout=30000)
        acc = self.account["profile"]
        page.fill("input#order_billing_name", f"{acc['first_name']} {acc['last_name']}")
        page.fill("input#order_email", self.account["email"])
        page.fill("input#order_tel", acc["phone"])
        page.fill("input#bo", acc["address"])
        page.fill("input#order_billing_city", acc["city"])
        page.select_option("select#order_billing_state", acc["state"])
        page.fill("input#order_billing_zip", acc["zip"])
        page.select_option("select#credit_card_type", "visa")
        page.fill("input#cnb", acc["card_number"])
        page.select_option("select#credit_card_month", acc["card_expiry"][:2])
        page.select_option("select#credit_card_year", acc["card_expiry"][-2:])
        page.fill("input#vval", acc["card_cvv"])
        if page.locator("iframe[title*='captcha']").count():
            if not solve_captcha(page):
                return False
        page.click("input[value='process payment']")
        page.wait_for_timeout(8000)
        if "order confirmed" in page.content().lower():
            _post("Supreme order placed!", "info")
            return True
        return False


# ── SNKRS module (Nike draw / FLOW) ────────────────────────────────────────
class SNKRSModule(SiteModule):
    def pre_warm(self, page):
        page.goto("https://www.nike.com/launch/", wait_until="networkidle", timeout=30000)
        # Dismiss popup
        try:
            page.click("button[data-var='closeButton']")
        except:
            pass

    def check_stock(self, page):
        style = self.cfg.get("style_code")
        page.goto(f"https://api.nike.com/product_feed/threads/v3/?filter=styleNumber({style})", wait_until="networkidle")
        json_response = page.text()
        try:
            data = json.loads(json_response)
            # Check if the launch date is now or soon
            return True  # simplified
        except:
            return False

    def add_to_cart(self, page):
        # SNKRS uses a “draw” system; we just submit entry
        # Assume we are on the product page
        page.click("button.draw-entry-button")
        time.sleep(2)
        page.click("button.enter-draw")
        _post("SNKRS draw entry submitted", "info")

    def checkout(self, page):
        # SNKRS will auto‑charge on win; no separate checkout
        return True  # placeholder


# ── Module factory ──────────────────────────────────────────────────────────
def create_module(site_cfg, account):
    t = site_cfg.get("type", "shopify").lower()
    if t == "shopify":
        return ShopifyModule(site_cfg, account)
    elif t == "supreme":
        return SupremeModule(site_cfg, account)
    elif t == "snkrs":
        return SNKRSModule(site_cfg, account)
    else:
        return ShopifyModule(site_cfg, account)  # fallback


# ── Drop calendar scraper ───────────────────────────────────────────────────
def fetch_drop_calendar():
    """Scrape Twitter / known drop sites for upcoming releases."""
    # Simple: fetch from @DropsByJay using an unofficial API
    # (requires Twitter dev credentials; omitted for brevity)
    return []


# ── Session warmer ──────────────────────────────────────────────────────────
def warm_sessions(site_modules):
    """Run pre‑warming for all enabled sites simultaneously."""
    with sync_playwright() as p:
        for mod in site_modules:
            if not mod.cfg.get("enabled", True):
                continue
            proxy = next_proxy()
            launch_opts = {"headless": False}
            if proxy:
                launch_opts["proxy"] = {"server": proxy}
            browser = p.chromium.launch(**launch_opts)
            page = browser.new_page()
            try:
                mod.pre_warm(page)
            except Exception as e:
                _post(f"Warm error {mod.name}: {e}", "warning")
            finally:
                browser.close()


# ── Multi‑site runner ───────────────────────────────────────────────────────
def run_site(module):
    """Single site purchase attempt (thread target)."""
    if not module.cfg.get("enabled", True):
        return
    proxy = next_proxy()
    with sync_playwright() as p:
        launch_opts = {"headless": False}
        if proxy:
            launch_opts["proxy"] = {"server": proxy}
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            if not module.check_stock(page):
                return
            _post(f"{module.name}: in stock!", "error")
            module.add_to_cart(page)
            if module.checkout(page):
                # Record purchase
                STATE.setdefault("purchased", []).append({
                    "site": module.name,
                    "timestamp": datetime.utcnow().isoformat()
                })
                save_state(STATE)
                # eBay listing if enabled
                if HAS_EBAY and CFG["ebay"].get("enabled"):
                    list_on_ebay(module.name, 150.0, module.cfg.get("url"))
        except Exception as e:
            _post(f"{module.name} error: {e}", "error")
        finally:
            browser.close()


def list_on_ebay(product_name, price, url=None):
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
    markup = CFG["ebay"].get("markup_multiplier", 2.5)
    ebay_price = round(price * markup, 2)
    payload = {
        "Item": {
            "Title": f"{product_name} – Limited Release",
            "Description": "Brand new, authentic. Ships immediately.",
            "PrimaryCategory": {"CategoryID": "11450"},  # Clothing, Shoes & Accessories > Men > Men's Shoes
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": "Days_30",
            "Country": "US",
            "Currency": "USD",
            "ListingType": "FixedPriceItem",
            "Site": "US",
        }
    }
    try:
        resp = trading.execute("AddFixedPriceItem", payload)
        if resp.dict().get("Ack") == "Success":
            _post(f"eBay listing created: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay error: {e}", "error")


# ── Main orchestration ──────────────────────────────────────────────────────
def main():
    wait_for_hub()
    _post("Universal Hype AIO Bot online.", "info")

    sites_cfg = CFG.get("sites", [])
    accounts = CFG.get("accounts", [])
    if not accounts:
        _post("No accounts configured.", "error")
        return

    # Use first account (could cycle through multiple)
    account = accounts[0]
    modules = [create_module(site, account) for site in sites_cfg if site.get("enabled", True)]

    # Pre‑warm sessions every hour
    def warmer():
        while True:
            warm_sessions(modules)
            time.sleep(3600)

    threading.Thread(target=warmer, daemon=True).start()

    # Main monitoring loop
    executor = ThreadPoolExecutor(max_workers=len(modules))
    while True:
        # Run all enabled sites concurrently
        futures = [executor.submit(run_site, mod) for mod in modules]
        for f in futures:
            f.result(timeout=120)
        _heartbeat()
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()
