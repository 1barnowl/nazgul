#!/usr/bin/env python3
"""
usj_express_pass_bot.py — Universal Studios Japan Express Pass 7 Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors USJ ticketing site for Express Pass 7 availability.
2. Auto‑selects desired date and pass type.
3. Logs into USJ Club account, fills guest details, and checks out.
4. Rotates proxies and solves CAPTCHAs via 2Captcha.
5. Optionally lists purchased passes on eBay (configurable).

✦ This is an educational / research bot.
  Automating purchases on USJ’s official site may breach their ToS.
  Use only on accounts you own for study purposes.

SETUP
─────
1. Install dependencies:
      pip install playwright requests beautifulsoup4 ebaysdk
      python -m playwright install chromium

2. Export 2Captcha API key:
      export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `usj_express_config.json` (example at bottom).
   Fill in:
   - USJ Club account login.
   - Desired pass type (Express Pass 7), date(s), and guest info.
   - Payment card details.

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
BOT_ID   = "usj_express_pass_bot"
BOT_NAME = "USJ Express Pass Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "usj_express_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "usj_express_state.json")

SCAN_INTERVAL      = 30    # seconds between availability checks
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

# ── Configuration ─────────────────────────────────────────────────────────────
def load_config():
    default = {
        "pass_type": "Express Pass 7 ~ Variety Choice",
        "target_date": "2026-06-15",  # YYYY-MM-DD
        "quantity": 1,
        "usj_account": {
            "email": "your_usjclub@email.com",
            "password": "your_password"
        },
        "guests": [
            {
                "first_name": "Taro",
                "last_name": "Yamada",
                "gender": "Male",
                "birthdate": "1990-01-01"
            }
        ],
        "payment": {
            "card_number": "4111111111111111",
            "card_expiry": "12/28",
            "card_cvv": "123",
            "cardholder_name": "TARO YAMADA"
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {
            "enabled": False,
            "markup_multiplier": 2.0,
            "auto_list": False
        }
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── State ─────────────────────────────────────────────────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"purchased_dates": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Proxy rotation ────────────────────────────────────────────────────────────
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

# ── CAPTCHA solver (2Captcha) ─────────────────────────────────────────────────
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

# ── USJ Express Pass Purchase (Playwright) ────────────────────────────────────
def attempt_usj_purchase():
    """
    Automates the USJ Express Pass 7 purchase flow:
    - Opens USJ ticketing site (English version)
    - Selects Express Pass 7, desired date, and quantity
    - Logs into USJ Club account
    - Fills guest details and payment
    - Completes purchase
    Returns True if successful.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed.", "error")
        return False

    account = CFG["usj_account"]
    pass_type = CFG["pass_type"]
    target_date = CFG["target_date"]
    guests = CFG["guests"]
    payment = CFG["payment"]

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy:
        launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        context = browser.new_context()
        page = context.new_page()

        try:
            # ── 1. Open USJ Express Pass page ──────────────────────────────────
            base_url = "https://www.usj.co.jp/web/en/us/tickets/express-pass/express-pass-7-variety-choice"
            _post("Navigating to Express Pass 7 page...", "info")
            page.goto(base_url, wait_until="networkidle", timeout=30000)

            # Some redirection to Japanese? We'll try to click the English link if needed
            if "/ja/" in page.url:
                # Switch to English (usually a button/flag)
                _post("Switching to English...", "info")
                page.click("a[hreflang='en'], a:has-text('EN')")
                page.wait_for_load_state("networkidle")

            # ── 2. Check availability and select date ──────────────────────────
            # USJ's site often uses a calendar widget; we need to pick the target date
            _post(f"Selecting date {target_date}...", "info")
            # Click on "Buy Now" or "Select Date" button
            buy_btn = page.locator("a:has-text('Buy Now'), button:has-text('Buy Now')")
            if buy_btn.count():
                buy_btn.click()
                page.wait_for_load_state("networkidle")
            else:
                # Maybe we are already on a calendar page
                pass

            # Wait for calendar to be visible
            page.wait_for_selector("div.calendar, table.calendar", timeout=10000)
            # Find the cell for target_date. Format example: 2026-06-15 -> June 15, 2026
            # The calendar usually has date cells with data-date attribute or aria-label
            target_cell = page.locator(f"td[data-date='{target_date}'], td[aria-label*='{target_date}']")
            if target_cell.count() == 0:
                # Fallback: click on the day number in the correct month
                date_parts = target_date.split("-")
                month_year = f"{date_parts[0]}-{date_parts[1]}"
                # Navigate to correct month if not already
                # Look for month label, click next/prev as needed
                # For simplicity, assume current month matches; we can add logic later
                day = str(int(date_parts[2]))  # remove leading zero
                target_cell = page.locator(f"td:has-text('{day}')")
            if target_cell.count() and target_cell.is_visible():
                target_cell.click()
                _post(f"Date {target_date} selected.", "info")
            else:
                _post("Could not select date. Possibly sold out.", "warning")
                return False

            # ── 3. Select pass type (Express Pass 7) ───────────────────────────
            page.wait_for_timeout(2000)
            # Choose the specific pass if multiple options (e.g., Variety Choice)
            pass_selector = f"div.pass-type:has-text('{pass_type}') input, label:has-text('{pass_type}') input"
            pass_radio = page.locator(pass_selector)
            if pass_radio.count():
                pass_radio.check()
                _post(f"Selected {pass_type}.", "info")
            else:
                # Maybe there's only one type; just continue
                pass

            # ── 4. Click "Add to Cart" or "Proceed" ───────────────────────────
            proceed_btn = page.locator("button:has-text('Proceed'), a:has-text('Add to Cart')")
            page.click(proceed_btn)
            page.wait_for_load_state("networkidle")

            # ── 5. Log in / continue as guest guest? USJ may require account. ──
            if "login" in page.url.lower() or page.locator("input#loginEmail").is_visible():
                _post("Logging into USJ Club account...", "info")
                page.fill("input#loginEmail, input[name='email']", account["email"])
                page.fill("input#loginPassword, input[name='password']", account["password"])
                page.click("button:has-text('Sign In')")
                page.wait_for_load_state("networkidle")

            # ── 6. Fill guest information ──────────────────────────────────────
            _post("Filling guest details...", "info")
            for idx, guest in enumerate(guests):
                # Many Japanese sites use separate fields per guest
                prefix = f"guest{idx+1}"
                page.fill(f"input[name='{prefix}.firstName']", guest["first_name"])
                page.fill(f"input[name='{prefix}.lastName']", guest["last_name"])
                if page.locator(f"select[name='{prefix}.gender']").count():
                    page.select_option(f"select[name='{prefix}.gender']", guest["gender"])
                page.fill(f"input[name='{prefix}.birthDate']", guest["birthdate"])

            # ── 7. Payment details ────────────────────────────────────────────
            if page.locator("input[name='cardNumber']").is_visible():
                _post("Entering payment...", "info")
                page.fill("input[name='cardNumber']", payment["card_number"])
                page.fill("input[name='expiry']", payment["card_expiry"])
                page.fill("input[name='cvv']", payment["card_cvv"])
                page.fill("input[name='cardholder']", payment["cardholder_name"])

            # ── 8. CAPTCHA? ───────────────────────────────────────────────────
            if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    return False
                page.wait_for_timeout(2000)

            # ── 9. Confirm and purchase ───────────────────────────────────────
            confirm_btn = page.locator("button:has-text('Purchase'), button:has-text('Confirm')")
            if confirm_btn.is_visible():
                confirm_btn.click()
                _post("Purchase confirmed.", "info")
            else:
                _post("Could not find final purchase button.", "error")
                return False

            page.wait_for_timeout(10000)

            # Check for success
            if "thank you" in page.content().lower() or "order complete" in page.content().lower():
                _post(f"✅ Successfully purchased Express Pass 7 for {target_date}!", "info")
                return True
            else:
                _post("Purchase may have failed – check manually.", "warning")
                return False
        except Exception as e:
            _post(f"USJ purchase error: {e}", "error")
            return False
        finally:
            browser.close()

# ── eBay listing (optional) ──────────────────────────────────────────────────
def list_on_ebay(pass_type, date, purchase_price):
    if not HAS_EBAY:
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
    markup = CFG["ebay"]["markup_multiplier"]
    ebay_price = round(purchase_price * markup, 2)
    title = f"Universal Studios Japan Express Pass 7 – {date} – Guaranteed"
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": f"Guaranteed Express Pass 7 for Universal Studios Japan on {date}. Instant delivery of QR code.",
            "PrimaryCategory": {"CategoryID": "170067"},  # Tickets & Experiences > Theme Park & Club Passes
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
        response = trading.execute("AddFixedPriceItem", payload)
        if response.dict().get("Ack") == "Success":
            item_id = response.dict()["ItemID"]
            _post(f"eBay listing created: {item_id} at ${ebay_price}", "info")
            return item_id
    except Exception as e:
        _post(f"eBay listing error: {e}", "error")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    state = load_state()

    _post("USJ Express Pass Bot online – monitoring inventory...", "info")

    while True:
        # Prevent duplicate purchase for the same date/pass type within 7 days
        key = f"{CFG['pass_type']}_{CFG['target_date']}"
        already = any(
            p.get("key") == key and 
            (datetime.utcnow() - datetime.fromisoformat(p["timestamp"])) < timedelta(days=7)
            for p in state.get("purchased_dates", [])
        )
        if already:
            _post("Already purchased this pass/date recently. Waiting...", "info")
        else:
            success = attempt_usj_purchase()
            if success:
                state.setdefault("purchased_dates", []).append({
                    "key": key,
                    "timestamp": datetime.utcnow().isoformat(),
                    "price_paid": 120.0  # approximate retail in USD
                })
                save_state(state)

                if CFG["ebay"].get("auto_list"):
                    list_on_ebay(CFG["pass_type"], CFG["target_date"], 120.0)

        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `usj_express_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "pass_type": "Express Pass 7 ~ Variety Choice",
  "target_date": "2026-06-15",
  "quantity": 1,
  "usj_account": {
    "email": "your_usjclub@email.com",
    "password": "your_password"
  },
  "guests": [
    {
      "first_name": "Taro",
      "last_name": "Yamada",
      "gender": "Male",
      "birthdate": "1990-01-01"
    }
  ],
  "payment": {
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "cardholder_name": "TARO YAMADA"
  },
  "proxies": { "list": [] },
  "captcha": { "api_key": "YOUR_2CAPTCHA_KEY" },
  "ebay": {
    "enabled": false,
    "markup_multiplier": 2.0,
    "auto_list": false
  }
}
"""
