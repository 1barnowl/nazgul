#!/usr/bin/env python3
"""
exclusive_chefs_table_bot.py — Exclusive Chef’s Table Reservation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors ultra‑limited omakase / chef’s table releases (Tock, Resy, etc.).
2. Instantly books the moment availability appears, filling in guest + payment.
3. Resells the confirmed reservation via eBay or collects a fee via Stripe.
4. Reports everything to BotController.

✦ Uses Playwright + 2Captcha for real browser automation.
  For educational & research purposes only. Automating bookings may
  violate restaurant policies and terms of service.

SETUP
─────
1. Install dependencies:
      pip install playwright ebaysdk stripe requests
      python -m playwright install chromium

2. Set environment variables:
      export CAPTCHA_API_KEY="your-2captcha-key"
      export STRIPE_SECRET_KEY="sk_test_..."        (optional)
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID,
             EBAY_AUTH_TOKEN                           (optional)

3. Create `chefs_table_config.json` (example at bottom).
   Configure:
   - Restaurant details, Tock/Resy URLs, selectors.
   - Client information (or just placeholder for resale).
   - Payment details for the restaurant booking.
   - Resale platform preferences.

4. Attach to BotController.
"""

import json, os, re, time, uuid, threading, requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

try:
    from ebaysdk.trading import Connection as eBayTrading
    HAS_EBAY = True
except ImportError:
    HAS_EBAY = False

# ═══════════════════════════════════════════════════════════════════════════
# BotController integration
HUB      = "http://localhost:8765"
BOT_ID   = "exclusive_chefs_table_bot"
BOT_NAME = "Chef’s Table Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chefs_table_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chefs_table_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 15   # seconds – frequent checks right before release

_last_hb = 0.0
_lock = threading.Lock()

def _post(summary, level="info", payload=None):
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID, "bot_name": BOT_NAME,
            "summary": summary, "level": level, "payload": payload or {}
        }, timeout=5)
    except: pass

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
    except: pass

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).ok: return
        except: pass
        time.sleep(1)

# ═══════════════════════════════════════════════════════════════════════════
# Config & State
def load_config():
    default = {
        "restaurant": {
            "name": "Sushi Nakazawa",
            "platform": "tock",
            "base_url": "https://www.exploretock.com/sushinakazawa",
            "experience_url": "https://www.exploretock.com/sushinakazawa/chefs-table",
            "release_time_utc": "2026-07-01T15:00:00Z",  # optional – bot will wait if set
            "selectors": {
                "availability_check": "button.available-slot, .reservation-card:not(.sold-out)",
                "time_slot": "button.time",
                "guest_count": "select[name='party_size']",
                "guest_first_name": "input[name='first_name']",
                "guest_last_name": "input[name='last_name']",
                "guest_email": "input[name='email']",
                "guest_phone": "input[name='phone']",
                "special_requests": "textarea[name='requests']",
                "continue_button": "button.continue",
                "confirm_button": "button.confirm-booking",
                "credit_card_number": "input[name='cardnumber']",
                "credit_card_expiry": "input[name='exp-date']",
                "credit_card_cvc": "input[name='cvc']",
                "place_order": "button.submit-booking"
            },
            "pre_payment_required": True
        },
        "booking": {
            "client_name": "Aiko Tanaka",
            "client_email": "aiko@example.com",
            "client_phone": "+81-90-1234-5678",
            "party_size": 2,
            "preferred_times": ["18:00", "19:00", "20:00"],
            "special_requests": "Celebrating anniversary"
        },
        "payment": {
            "card_number": "4111111111111111",
            "card_expiry": "12/28",
            "card_cvv": "123",
            "cardholder_name": "Aiko Tanaka",
            "billing_zip": "10001"
        },
        "resale": {
            "ebay": {
                "enabled": True,
                "markup_price_usd": 500.0,   # auction starting price or fixed price
                "listing_duration_days": 10
            },
            "stripe": {
                "enabled": False,
                "fee_usd": 250.0
            }
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", "")}
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"bookings": []}
    with open(STATE_FILE, "r") as f: return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ── Proxy & Captcha ────────────────────────────────────────────────────────
_proxies = CFG.get("proxies", {}).get("list", [])
_proxy_idx = 0
def next_proxy():
    global _proxy_idx
    if not _proxies: return None
    with _lock:
        p = _proxies[_proxy_idx % len(_proxies)]
        _proxy_idx += 1; return p

CAPTCHA_KEY = CFG.get("captcha", {}).get("api_key", "")
def solve_captcha(page, sitekey=None):
    if not CAPTCHA_KEY: return False
    try:
        if not sitekey:
            el = page.locator("[data-sitekey]")
            if el.count(): sitekey = el.get_attribute("data-sitekey")
            else: return False
        resp = requests.get("http://2captcha.com/in.php", params={
            "key": CAPTCHA_KEY, "method": "userrecaptcha",
            "googlekey": sitekey, "pageurl": page.url, "json": 1
        }, timeout=15).json()
        if resp.get("status") != 1: return False
        cid = resp["request"]
        for i in range(30):
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
            if r.get("request") != "CAPCHA_NOT_READY": break
    except: pass
    return False

# ── Resale functions ──────────────────────────────────────────────────────
def list_on_ebay(restaurant_name, date_time, party_size, confirmation_code, price):
    if not HAS_EBAY or not CFG["resale"]["ebay"]["enabled"]: return
    trading = eBayTrading(
        domain="api.ebay.com",
        appid=os.getenv("EBAY_APP_ID"), certid=os.getenv("EBAY_CERT_ID"),
        devid=os.getenv("EBAY_DEV_ID"), token=os.getenv("EBAY_AUTH_TOKEN"),
        config_file=None
    )
    if not trading: return
    markup = CFG["resale"]["ebay"]["markup_price_usd"]
    ebay_price = markup
    title = f"Exclusive {restaurant_name} Chef’s Table – {date_time} (Party of {party_size})"
    description = f"Confirmed reservation for {party_size} at {restaurant_name} on {date_time}. Will transfer immediately. Confirmation: {confirmation_code}"
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": description,
            "PrimaryCategory": {"CategoryID": "170067"},   # Tickets & Experiences
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": f"Days_{CFG['resale']['ebay']['listing_duration_days']}",
            "Country": "US", "Currency": "USD",
            "ListingType": "FixedPriceItem", "Site": "US"
        }
    }
    try:
        resp = trading.execute("AddFixedPriceItem", payload)
        if resp.dict().get("Ack") == "Success":
            _post(f"eBay auction created: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay error: {e}", "error")

def charge_stripe_fee(client_email, fee_usd):
    if not CFG["resale"]["stripe"]["enabled"]: return True
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key: return False
    try:
        stripe.PaymentIntent.create(
            amount=int(fee_usd * 100),
            currency="usd",
            description=f"Chef’s Table reservation fee",
            metadata={"client_email": client_email}
        )
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# Core booking flow for Tock (can be extended to Resy, etc.)
def book_chefs_table():
    """
    Monitor the restaurant's Tock page for the chef's table experience.
    If the release time is set, wait until that time before checking.
    Then continually scan for availability, and immediately book when found.
    Return confirmation code/ID or None.
    """
    cfg = CFG["restaurant"]
    booking = CFG["booking"]
    payment = CFG["payment"]

    # Optional: wait until release time
    release_str = cfg.get("release_time_utc")
    if release_str:
        try:
            release_dt = datetime.fromisoformat(release_str.replace("Z", "+00:00"))
            now_utc = datetime.utcnow().replace(tzinfo=release_dt.tzinfo)
            if now_utc < release_dt:
                wait_seconds = (release_dt - now_utc).total_seconds()
                _post(f"Waiting {wait_seconds/60:.1f} minutes until release...", "info")
                time.sleep(min(wait_seconds, 3 * 3600))  # sleep up to 3 hours, then proceed
        except Exception as e:
            _post(f"Error parsing release time: {e}", "warning")

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy: launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            url = cfg["experience_url"]
            _post(f"Opening {url}", "info")
            page.goto(url, wait_until="networkidle", timeout=30000)

            # Wait for availability to appear (poll up to 30 minutes)
            start = time.time()
            while time.time() - start < 1800:   # 30 minutes
                # Check for the presence of an available slot button
                availability_sel = cfg["selectors"]["availability_check"]
                if page.locator(availability_sel).count() == 0:
                    # No slots visible – refresh page and wait
                    _post("No availability yet, refreshing...", "info")
                    time.sleep(3)
                    page.reload(wait_until="networkidle")
                    continue

                # A slot is available – click the first one
                slot_btn = page.locator(availability_sel).first
                slot_btn.click()
                _post("Selected an available time slot.", "info")
                break
            else:
                _post("No availability found within 30 minutes. Aborting.", "warning")
                return None

            # Select party size if needed
            sel = cfg["selectors"]
            if sel.get("guest_count"):
                page.select_option(sel["guest_count"], str(booking["party_size"]))

            # Fill guest details
            page.fill(sel["guest_first_name"], booking["client_name"].split()[0])
            page.fill(sel["guest_last_name"], booking["client_name"].split()[-1] if len(booking["client_name"].split()) > 1 else booking["client_name"])
            page.fill(sel["guest_email"], booking["client_email"])
            page.fill(sel["guest_phone"], booking["client_phone"])
            if sel.get("special_requests"):
                page.fill(sel["special_requests"], booking.get("special_requests", ""))

            # Click continue
            page.click(sel["continue_button"])
            page.wait_for_load_state("networkidle")

            # Pre-payment (many top restaurants require credit card to hold)
            if cfg.get("pre_payment_required"):
                _post("Filling payment details...", "info")
                page.fill(sel["credit_card_number"], payment["card_number"])
                page.fill(sel["credit_card_expiry"], payment["card_expiry"])
                page.fill(sel["credit_card_cvc"], payment["card_cvv"])
                if sel.get("credit_card_zip"):
                    page.fill(sel["credit_card_zip"], payment["billing_zip"])

            # Handle CAPTCHA
            if page.locator("[data-sitekey]").count() or page.locator("iframe[title*='captcha']").count():
                if not solve_captcha(page):
                    return None
                time.sleep(2)

            # Final confirm
            confirm_btn = sel["confirm_button"]
            page.click(confirm_btn)
            page.wait_for_timeout(5000)

            # Submit payment / place order
            if sel.get("place_order"):
                page.click(sel["place_order"])
                page.wait_for_timeout(5000)

            # Check confirmation
            content = page.content().lower()
            if "thank you" in content or "confirmation" in content or "reservation confirmed" in content:
                conf_match = re.search(r'(?:confirmation|reservation)\s*(?:#|:)\s*([A-Z0-9-]+)', content)
                confirmation = conf_match.group(1) if conf_match else "BOOKED"
                _post(f"🎉 Chef’s Table secured! Confirmation: {confirmation}", "info")
                return confirmation
            else:
                _post("Booking may have failed – check manually.", "warning")
                return "POSSIBLY_BOOKED"
        except Exception as e:
            _post(f"Booking error: {e}", "error")
            return None
        finally:
            browser.close()

# ═══════════════════════════════════════════════════════════════════════════
# Main loop
def run():
    state = load_state()
    # Only book if haven't already succeeded (or limit one booking)
    key = CFG["restaurant"]["name"] + "_" + CFG["booking"]["client_email"]
    if any(b.get("key") == key for b in state.get("bookings", [])):
        _post("Already booked for this client/restaurant. Skipping.", "info")
        return

    confirmation = book_chefs_table()
    if confirmation:
        state.setdefault("bookings", []).append({
            "key": key,
            "restaurant": CFG["restaurant"]["name"],
            "date_time": "Unknown",   # ideally extract from page
            "party": CFG["booking"]["party_size"],
            "confirmation": confirmation,
            "timestamp": datetime.utcnow().isoformat()
        })
        save_state(state)

        # Resale
        client_email = CFG["booking"]["client_email"]
        if CFG["resale"]["ebay"]["enabled"]:
            list_on_ebay(CFG["restaurant"]["name"], "Reserved", CFG["booking"]["party_size"],
                        confirmation, CFG["resale"]["ebay"]["markup_price_usd"])
        if CFG["resale"]["stripe"]["enabled"]:
            charge_stripe_fee(client_email, CFG["resale"]["stripe"]["fee_usd"])

def main():
    wait_for_hub()
    _post("Exclusive Chef’s Table Bot online. Preparing for release...", "info")
    while True:
        run()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `chefs_table_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "restaurant": {
    "name": "Sushi Nakazawa",
    "platform": "tock",
    "base_url": "https://www.exploretock.com/sushinakazawa",
    "experience_url": "https://www.exploretock.com/sushinakazawa/chefs-table",
    "release_time_utc": "2026-07-01T15:00:00Z",
    "selectors": {
      "availability_check": "button.available-slot, .reservation-card:not(.sold-out)",
      "time_slot": "button.time",
      "guest_count": "select[name='party_size']",
      "guest_first_name": "input[name='first_name']",
      "guest_last_name": "input[name='last_name']",
      "guest_email": "input[name='email']",
      "guest_phone": "input[name='phone']",
      "special_requests": "textarea[name='requests']",
      "continue_button": "button.continue",
      "confirm_button": "button.confirm-booking",
      "credit_card_number": "input[name='cardnumber']",
      "credit_card_expiry": "input[name='exp-date']",
      "credit_card_cvc": "input[name='cvc']",
      "place_order": "button.submit-booking"
    },
    "pre_payment_required": true
  },
  "booking": {
    "client_name": "Aiko Tanaka",
    "client_email": "aiko@example.com",
    "client_phone": "+81-90-1234-5678",
    "party_size": 2,
    "preferred_times": ["18:00", "19:00"],
    "special_requests": "Anniversary celebration"
  },
  "payment": {
    "card_number": "4111111111111111",
    "card_expiry": "12/28",
    "card_cvv": "123",
    "cardholder_name": "Aiko Tanaka",
    "billing_zip": "10001"
  },
  "resale": {
    "ebay": {"enabled": true, "markup_price_usd": 500, "listing_duration_days": 10},
    "stripe": {"enabled": false, "fee_usd": 250}
  },
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"}
}
"""
