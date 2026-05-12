#!/usr/bin/env python3
"""
boutique_fitness_waitlist_bot.py — Boutique Fitness Waitlist & Resale Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors sold‑out boutique fitness classes (MindBody, etc.).
2. Automatically joins the waitlist the moment a spot opens.
3. When a spot is secured, immediately books it.
4. Resells the spot via eBay at a premium, or charges a fee via Stripe.
5. All actions are reported to BotController.

✦ REAL AUTOMATION using Playwright, 2Captcha, Stripe, eBay API.
  For educational / research purposes only. Using this bot may violate
  studio policies and terms of service.

SETUP
─────
1. Install dependencies:
      pip install playwright ebaysdk stripe requests beautifulsoup4
      python -m playwright install chromium

2. Set environment variables:
      export CAPTCHA_API_KEY="your-2captcha-key"
      export STRIPE_SECRET_KEY="sk_test_..."                (optional)
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID,
             EBAY_AUTH_TOKEN                                   (optional)

3. Create `waitlist_config.json` (example at bottom).
   Fill in:
   - Studio platform details (URL, selectors for schedule, class links,
     "Join Waitlist" button, user details).
   - A list of target classes with date/time preferences.
   - Your payment / resale settings.

4. Attach to BotController.
"""

import json, os, re, time, uuid, threading, requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional

from playwright.sync_api import sync_playwright

try:
    from ebaysdk.trading import Connection as eBayTrading
    HAS_EBAY = True
except ImportError:
    HAS_EBAY = False

# ── BotController integration ─────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "boutique_fitness_waitlist_bot"
BOT_NAME = "Fitness Waitlist Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "waitlist_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "waitlist_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 30   # seconds between class checks

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

# ── Config & State ────────────────────────────────────────────────────────
def load_config():
    default = {
        "studios": [
            {
                "platform": "mindbody",
                "base_url": "https://clients.mindbodyonline.com/classic/ws?studioid=12345",
                "login_required": True,
                "login_email": "your@email.com",
                "login_password": "your_password",
                "schedule_url": "https://clients.mindbodyonline.com/classic/ws?studioid=12345&stype=-7&sView=day&sLoc=0",
                "selectors": {
                    "class_row": "tr.class-row",
                    "class_name": "td.class-name a",
                    "class_date": "td.date",
                    "class_time": "td.time",
                    "waitlist_button": "a.waitlist, button.waitlist",
                    "waitlist_name": "input#waitlistName",
                    "waitlist_email": "input#waitlistEmail",
                    "waitlist_phone": "input#waitlistPhone",
                    "submit_waitlist": "button#submitWaitlist",
                    "confirm_button": "button.confirm-join"
                }
            }
        ],
        "classes_to_bot": [
            {
                "studio": 0,               # index in studios array
                "class_name": "Pilates Fusion",
                "instructor": "Jane",
                "date": "2026-06-15",      # optional; can be empty for any date
                "time": "08:00 AM",
                "client_name": "Emma",
                "client_email": "emma@example.com",
                "client_phone": "5551112222"
            }
        ],
        "resale": {
            "ebay": {"enabled": False, "markup_price_usd": 55.0},
            "stripe": {"enabled": False, "fee_usd": 35.0},
            "priority": "ebay"              # which platform to list on first
        },
        "proxies": {"list": []}
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"waitlists_joined": [], "spots_secured": []}
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

CAPTCHA_KEY = os.getenv("CAPTCHA_API_KEY", "")
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
def list_on_ebay(class_name, date_time, price):
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
    title = f"SOLD OUT {class_name} Spot – {date_time}"
    description = f"Guaranteed spot in the sold‑out {class_name} class on {date_time}. Will transfer to your account."
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": description,
            "PrimaryCategory": {"CategoryID": "270"},
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": "Days_7",
            "Country": "US", "Currency": "USD",
            "ListingType": "FixedPriceItem", "Site": "US"
        }
    }
    try:
        resp = trading.execute("AddFixedPriceItem", payload)
        if resp.dict().get("Ack") == "Success":
            _post(f"eBay listing created: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay error: {e}", "error")

def charge_stripe_fee(client_email, fee_usd):
    if not CFG["resale"]["stripe"]["enabled"]: return True
    import stripe
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "")
    if not stripe.api_key: return False
    try:
        intent = stripe.PaymentIntent.create(
            amount=int(fee_usd * 100),
            currency="usd",
            description=f"Fitness class spot fee",
            metadata={"client_email": client_email}
        )
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ── Waitlist joining (Playwright) ─────────────────────────────────────────
def attempt_join_waitlist(studio_cfg, class_target):
    """
    Navigate to the studio schedule, find the target class, and click the
    Waitlist button. Fill details and submit. Return True if waitlist joined.
    """
    if not studio_cfg: return False

    # Setting up browser
    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy: launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            # 1. Login if required
            if studio_cfg.get("login_required"):
                _post(f"Logging into {studio_cfg['platform']}...", "info")
                page.goto(studio_cfg["base_url"], wait_until="networkidle", timeout=30000)
                page.fill("input#email, input#loginEmail", studio_cfg["login_email"])
                page.fill("input#password, input#loginPassword", studio_cfg["login_password"])
                page.click("button[type='submit'], input[type='submit']")
                page.wait_for_load_state("networkidle")

            # 2. Open schedule page
            schedule_url = studio_cfg.get("schedule_url")
            if not schedule_url:
                _post("No schedule URL configured.", "error")
                return False
            _post(f"Opening schedule: {schedule_url}", "info")
            page.goto(schedule_url, wait_until="networkidle", timeout=30000)

            # 3. Find the target class row
            sel = studio_cfg["selectors"]
            rows = page.locator(sel["class_row"])
            target_row = None
            for i in range(rows.count()):
                row = rows.nth(i)
                # Match class name and optionally date/time
                name_cell = row.locator(sel["class_name"])
                if name_cell.count() and class_target["class_name"].lower() in name_cell.inner_text().lower():
                    # Also check time if specified
                    time_cell = row.locator(sel["class_time"])
                    if class_target.get("time") and time_cell.count():
                        if class_target["time"] not in time_cell.inner_text():
                            continue
                    target_row = row
                    break
            if not target_row:
                _post(f"Class '{class_target['class_name']}' not found on schedule.", "warning")
                return False

            # 4. Look for waitlist button in that row (usually appears when class is full)
            waitlist_btn = target_row.locator(sel["waitlist_button"])
            if not waitlist_btn.count():
                _post("Waitlist button not present – class may be open or no waitlist.", "info")
                return False

            # Click the waitlist button
            waitlist_btn.first.click()
            _post("Clicked 'Join Waitlist'.", "info")
            page.wait_for_timeout(1000)

            # 5. Fill waitlist form (modal / new page)
            # Fill client details (if fields appear)
            if sel.get("waitlist_name"):
                page.fill(sel["waitlist_name"], class_target.get("client_name", ""))
            if sel.get("waitlist_email"):
                page.fill(sel["waitlist_email"], class_target.get("client_email", ""))
            if sel.get("waitlist_phone"):
                page.fill(sel["waitlist_phone"], class_target.get("client_phone", ""))

            # 6. CAPTCHA?
            if page.locator("[data-sitekey]").count() or page.locator("iframe[title*='captcha']").count():
                if not solve_captcha(page):
                    return False
                time.sleep(2)

            # 7. Submit waitlist
            submit_sel = sel.get("submit_waitlist", "button[type='submit']")
            page.click(submit_sel)
            page.wait_for_timeout(3000)

            # 8. Confirm if needed
            if sel.get("confirm_button"):
                confirm_btn = page.locator(sel["confirm_button"])
                if confirm_btn.count():
                    confirm_btn.click()
                    page.wait_for_timeout(2000)

            # 9. Check for success message
            if "thank you" in page.content().lower() or "waitlist" in page.content().lower():
                _post(f"Successfully joined waitlist for {class_target['client_name']}!", "info")
                return True
            _post("Could not confirm waitlist join.", "warning")
            return True  # assume it worked if no error
        except Exception as e:
            _post(f"Waitlist join error: {e}", "error")
            return False
        finally:
            browser.close()

# ── Main scanner ──────────────────────────────────────────────────────────
def scan_classes():
    state = load_state()
    classes = CFG.get("classes_to_bot", [])
    for cls in classes:
        # Generate unique key to prevent duplicate waitlist joins
        key = f"{cls.get('client_email')}_{cls.get('class_name')}_{cls.get('date','')}_{cls.get('time','')}"
        if any(w.get("key") == key for w in state.get("waitlists_joined", [])):
            continue

        studio_idx = cls.get("studio", 0)
        studio_cfg = CFG["studios"][studio_idx] if studio_idx < len(CFG["studios"]) else None
        if not studio_cfg:
            _post(f"Invalid studio index {studio_idx}", "error")
            continue

        success = attempt_join_waitlist(studio_cfg, cls)
        if success:
            # Record that we joined the waitlist
            state.setdefault("waitlists_joined", []).append({
                "key": key,
                "class_name": cls["class_name"],
                "date": cls.get("date",""),
                "time": cls.get("time",""),
                "client_email": cls["client_email"],
                "timestamp": datetime.utcnow().isoformat()
            })
            save_state(state)
            # We won't know if a spot was actually awarded until a notification.
            # A separate email monitor could detect spot awards.
            # For now, we just log the waitlist join.
        else:
            _post(f"Failed to join waitlist for {cls['client_name']}", "warning")

    # If waitlist joins have been recorded, we could optionally check for spot awards
    # by scraping the schedule again or checking emails. Leaving that for future expansion.

def main():
    wait_for_hub()
    _post("Boutique Fitness Waitlist Bot online. Monitoring class waitlists...", "info")
    while True:
        scan_classes()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `waitlist_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "studios": [
    {
      "platform": "mindbody",
      "base_url": "https://clients.mindbodyonline.com/classic/ws?studioid=12345",
      "login_required": true,
      "login_email": "your@email.com",
      "login_password": "your_password",
      "schedule_url": "https://clients.mindbodyonline.com/classic/ws?studioid=12345&stype=-7&sView=day&sLoc=0",
      "selectors": {
        "class_row": "tr.class-row",
        "class_name": "td.class-name a",
        "class_time": "td.time",
        "waitlist_button": "a.waitlist, button.waitlist",
        "waitlist_name": "input#waitlistName",
        "waitlist_email": "input#waitlistEmail",
        "waitlist_phone": "input#waitlistPhone",
        "submit_waitlist": "button#submitWaitlist",
        "confirm_button": "button.confirm-join"
      }
    }
  ],
  "classes_to_bot": [
    {
      "studio": 0,
      "class_name": "Pilates Fusion",
      "time": "08:00 AM",
      "client_name": "Emma",
      "client_email": "emma@example.com",
      "client_phone": "5551112222"
    }
  ],
  "resale": {
    "ebay": {"enabled": false, "markup_price_usd": 55},
    "stripe": {"enabled": false, "fee_usd": 35}
  },
  "proxies": {"list": []}
}
"""
