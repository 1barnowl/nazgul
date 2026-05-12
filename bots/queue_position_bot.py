#!/usr/bin/env python3
"""
queue_position_bot.py — Queue Position Scalper Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Auto‑generates hundreds of unique emails (catch‑all domain).
2. Registers each email into a limited‑product queue months ahead.
3. Monitors inboxes for “you’ve been selected” emails.
4. When a slot activates, auto‑logs in, purchases the product,
   and optionally resells the queue spot on eBay.

✦ PURELY FOR EDUCATIONAL / RESEARCH PURPOSES.
  Automated mass registration and purchasing may violate
  retailer terms of service. Use only on authorized test systems.

SETUP
─────
1. Install dependencies:
      pip install playwright requests imaplib email bs4 ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. Set email credentials (IMAP) for monitoring inbox:
      export QUEUE_IMAP_USER="bot@yourdomain.com"
      export QUEUE_IMAP_PASS="yourpassword"

4. Create `queue_config.json` (example at bottom).
   Fill in:
   - Product details and queue registration URL.
   - Catch‑all domain for generating emails.
   - Account credentials for purchase (if needed).
   - Payment details.

5. Attach to BotController.
"""

import imaplib, email, json, os, re, time, random, threading, requests
from datetime import datetime, timedelta
from email.header import decode_header
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
BOT_ID   = "queue_position_bot"
BOT_NAME = "Queue Position Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue_state.json")

SCAN_INTERVAL      = 300   # email check interval (seconds)
HEARTBEAT_INTERVAL = 30

_lock = threading.Lock()
_last_hb = 0.0

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

# ── Config & State ──────────────────────────────────────────────────────────
def load_config():
    default = {
        "product": {
            "name": "EVGA RTX 4090 FTW3 Ultra",
            "url": "https://www.example.com/product/123",
            "price": 1599.99,
            "queue_registration_url": "https://www.example.com/queue/register"
        },
        "catch_all_domain": "yourdomain.com",   # e.g., @mail.yourdomain.com
        "emails_to_register": 100,
        "imap_server": "imap.yourdomain.com",
        "imap_user": os.getenv("QUEUE_IMAP_USER", "bot@yourdomain.com"),
        "imap_pass": os.getenv("QUEUE_IMAP_PASS", ""),
        "selection_email_subject_contains": "You’ve been selected",
        "purchase_account": {
            "email": "main_account@yourdomain.com",
            "password": "secure_password",
            "profile": {
                "first_name": "Jane", "last_name": "Doe",
                "address": "123 GPU Lane", "city": "San Jose", "state": "CA", "zip": "95112",
                "phone": "4085551234",
                "card_number": "4111111111111111",
                "card_expiry": "12/28", "card_cvv": "123"
            }
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {
            "enabled": False,
            "markup_multiplier": 1.5,
            "listing_mode": "queue_spot"   # or "physical_product"
        }
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"registered_emails": [], "purchased_slots": []}
    with open(STATE_FILE, "r") as f: return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ── Proxy & Captcha (standard) ──────────────────────────────────────────────
_proxies = CFG.get("proxies", {}).get("list", [])
_proxy_idx = 0
def next_proxy():
    global _proxy_idx
    if not _proxies: return None
    with _lock:
        p = _proxies[_proxy_idx % len(_proxies)]
        _proxy_idx += 1; return p

CAPTCHA_KEY = CFG.get("captcha", {}).get("api_key", "").strip()
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

# ── Email generation ────────────────────────────────────────────────────────
def generate_catch_all_emails(count, domain):
    """Generate a list of unique random email addresses using a catch‑all domain."""
    emails = []
    for i in range(count):
        local = f"queue{random.randint(10000, 99999)}{i}@{domain}"
        emails.append(local)
    return emails

# ── Queue registration (Playwright) ─────────────────────────────────────────
def register_emails(emails):
    """Register each email into the queue."""
    if not HAS_PLAYWRIGHT: return
    url = CFG["product"]["queue_registration_url"]
    for email_addr in emails:
        if email_addr in STATE.get("registered_emails", []):
            continue
        proxy = next_proxy()
        launch_opts = {"headless": False}
        if proxy: launch_opts["proxy"] = {"server": proxy}
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(**launch_opts)
                page = browser.new_page()
                page.goto(url, wait_until="networkidle", timeout=30000)
                # Fill email field (generic selectors; adjust per site)
                page.fill("input[type='email'], input[name='email']", email_addr)
                # Submit form
                page.click("button[type='submit'], input[type='submit']")
                page.wait_for_timeout(2000)
                # Optionally handle captcha
                if page.locator("[data-sitekey]").count():
                    solve_captcha(page)
                    page.click("button[type='submit']")  # retry
                    page.wait_for_timeout(2000)
                _post(f"Registered {email_addr}", "info")
                STATE.setdefault("registered_emails", []).append(email_addr)
                save_state(STATE)
            time.sleep(random.uniform(3, 8))
        except Exception as e:
            _post(f"Registration error for {email_addr}: {e}", "warning")

# ── Email inbox monitor (IMAP) ───────────────────────────────────────────
def check_inbox_for_selection():
    """Scan IMAP inbox for selection emails and return list of email addresses that won."""
    winners = []
    user = CFG["imap_user"]
    password = CFG["imap_pass"]
    server = CFG["imap_server"]
    if not user or not password:
        _post("IMAP credentials not set. Cannot check inbox.", "error")
        return []
    try:
        mail = imaplib.IMAP4_SSL(server)
        mail.login(user, password)
        mail.select("inbox")
        # Search unseen messages matching subject
        status, messages = mail.search(None, '(UNSEEN SUBJECT "selection")')
        if status == "OK":
            for num in messages[0].split():
                typ, msg_data = mail.fetch(num, "(RFC822)")
                for response_part in msg_data:
                    if isinstance(response_part, tuple):
                        msg = email.message_from_bytes(response_part[1])
                        subject, encoding = decode_header(msg["Subject"])[0]
                        if isinstance(subject, bytes):
                            subject = subject.decode(encoding if encoding else "utf-8")
                        if CFG["selection_email_subject_contains"].lower() in subject.lower():
                            # Extract the recipient email address (the queue email)
                            to_addresses = msg.get_all("to", [])
                            for addr in to_addresses:
                                addr_clean = email.utils.parseaddr(addr)[1]
                                if addr_clean and addr_clean.endswith(f"@{CFG['catch_all_domain']}"):
                                    winners.append(addr_clean)
                                    # Mark as seen/delete if needed
                                    mail.store(num, '+FLAGS', '\\Deleted')
        mail.expunge()
        mail.close()
        mail.logout()
    except Exception as e:
        _post(f"IMAP error: {e}", "error")
    return winners

# ── Purchase flow ──────────────────────────────────────────────────────────
def purchase_from_queue(winning_email):
    """
    Use the main purchase account to buy the product after selection.
    The winning email is usually the one that can now purchase; often a unique link is sent.
    We simulate by navigating to the product page and trying to add to cart (or follow email link).
    For research, we'll assume the product page becomes available after selection.
    """
    if not HAS_PLAYWRIGHT: return False
    account = CFG["purchase_account"]
    proxy = next_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        try:
            # Log in to purchase account
            _post("Logging into purchase account...", "info")
            page.goto("https://www.example.com/login", wait_until="networkidle", timeout=30000)
            page.fill("input#email", account["email"])
            page.fill("input#password", account["password"])
            page.click("button[type='submit']")
            page.wait_for_load_state("networkidle")

            # Navigate to product page (or use unique link from email – simplified here)
            page.goto(CFG["product"]["url"], wait_until="networkidle", timeout=30000)
            add_btn = page.locator("button:has-text('Add to Cart')")
            if add_btn.is_visible():
                add_btn.click()
                # Checkout flow (generic)
                page.goto("https://www.example.com/checkout", wait_until="networkidle")
                # Fill address/payment from profile
                _fill_checkout(page, account["profile"])
                if page.locator("[data-sitekey]").count():
                    solve_captcha(page)
                page.click("button:has-text('Place Order')")
                page.wait_for_timeout(8000)
                if "confirmation" in page.content().lower():
                    _post(f"Purchased product using queue slot from {winning_email}", "info")
                    return True
            return False
        except Exception as e:
            _post(f"Purchase error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_checkout(page, profile):
    # Generic checkout filler (Shopify-style)
    try:
        page.fill("input[name='email']", profile.get("email",""))
        page.fill("input[name='firstName']", profile["first_name"])
        page.fill("input[name='lastName']", profile["last_name"])
        page.fill("input[name='address1']", profile["address"])
        page.fill("input[name='city']", profile["city"])
        page.select_option("select[name='state']", profile["state"])
        page.fill("input[name='zip']", profile["zip"])
        page.fill("input[name='phone']", profile["phone"])
        page.fill("input[name='cardnumber']", profile["card_number"])
        page.fill("input[name='expiry']", profile["card_expiry"])
        page.fill("input[name='cvv']", profile["card_cvv"])
    except: pass

# ── Resell listing on eBay ─────────────────────────────────────────────────
def list_on_ebay(title, price):
    if not HAS_EBAY or not CFG["ebay"].get("enabled"): return
    trading = eBayTrading(
        domain="api.ebay.com",
        appid=os.getenv("EBAY_APP_ID"), certid=os.getenv("EBAY_CERT_ID"),
        devid=os.getenv("EBAY_DEV_ID"), token=os.getenv("EBAY_AUTH_TOKEN"),
        config_file=None
    )
    if not trading: return
    markup = CFG["ebay"]["markup_multiplier"]
    ebay_price = round(price * markup, 2)
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": f"Early queue position for {CFG['product']['name']}. Will transfer immediately upon purchase.",
            "PrimaryCategory": {"CategoryID": "171833"},  # Electronics
            "StartPrice": ebay_price,
            "Quantity": 1,
            "ListingDuration": "Days_30",
            "Country": "US", "Currency": "USD",
            "ListingType": "FixedPriceItem", "Site": "US"
        }
    }
    try:
        resp = trading.execute("AddFixedPriceItem", payload)
        if resp.dict().get("Ack") == "Success":
            _post(f"Queue spot listed on eBay: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay error: {e}", "error")

# ── Main coordinator ───────────────────────────────────────────────────────
def main():
    wait_for_hub()
    _post("Queue Position Bot online.", "info")

    # 1. Register emails if not already done
    needed = CFG["emails_to_register"]
    domain = CFG["catch_all_domain"]
    existing = len(STATE.get("registered_emails", []))
    remaining = max(0, needed - existing)
    if remaining > 0:
        _post(f"Registering {remaining} new emails...", "info")
        new_emails = generate_catch_all_emails(remaining, domain)
        register_emails(new_emails)

    # 2. Monitor inbox for winners
    while True:
        winners = check_inbox_for_selection()
        for email_winner in winners:
            if email_winner in [s["email"] for s in STATE.get("purchased_slots", [])]:
                continue
            _post(f"🎉 Queue selection for {email_winner}! Purchasing now...", "error")
            success = purchase_from_queue(email_winner)
            if success:
                STATE.setdefault("purchased_slots", []).append({
                    "email": email_winner,
                    "timestamp": datetime.utcnow().isoformat(),
                    "price": CFG["product"]["price"]
                })
                save_state(STATE)
                # Resell queue spot on eBay
                if CFG["ebay"].get("enabled"):
                    list_on_ebay(f"Queue Spot – {CFG['product']['name']}", CFG["product"]["price"] * 0.5)
            time.sleep(5)
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `queue_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "product": {
    "name": "EVGA RTX 4090 FTW3 Ultra",
    "url": "https://www.example.com/product/123",
    "price": 1599.99,
    "queue_registration_url": "https://www.example.com/queue/register"
  },
  "catch_all_domain": "yourdomain.com",
  "emails_to_register": 100,
  "imap_server": "imap.yourdomain.com",
  "imap_user": "bot@yourdomain.com",
  "imap_pass": "your_password",
  "selection_email_subject_contains": "You’ve been selected",
  "purchase_account": {
    "email": "main_account@yourdomain.com",
    "password": "secure_password",
    "profile": {
      "first_name": "Jane", "last_name": "Doe",
      "address": "123 GPU Lane", "city": "San Jose", "state": "CA", "zip": "95112",
      "phone": "4085551234",
      "card_number": "4111111111111111", "card_expiry": "12/28", "card_cvv": "123"
    }
  },
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"},
  "ebay": {
    "enabled": false,
    "markup_multiplier": 1.5,
    "listing_mode": "queue_spot"
  }
}
"""
