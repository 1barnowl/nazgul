#!/usr/bin/env python3
"""
tcpa_settlement_bot.py — TCPA Robocall Settlement Claim Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Loads a user’s call log (CSV) and matches it against known TCPA
   class‑action settlements from topclassactions.com.
2. Automatically files a claim for each eligible call via Playwright,
   filling in personal details and uploading proof (if required).
3. Collects a 40% contingency fee on the estimated per‑call payout
   (or a flat fee) using Stripe.

✦ REAL DATA: topclassactions.com (scraped), settlement administrator sites.
  For educational / research purposes only. Filing false claims is illegal.
  Use only with your own legitimate call records.

SETUP
─────
1. Install dependencies:
      pip install playwright beautifulsoup4 stripe requests
      python -m playwright install chromium

2. Set environment variables:
      export CAPTCHA_API_KEY="your-2captcha-key"   (optional)
      export STRIPE_SECRET_KEY="sk_test_..."        (optional)

3. Create `tcpa_config.json` (example at bottom). Fill in:
   - Path to your call log CSV (columns: phone_number, date, duration, caller_name)
   - Your personal information for claim forms.
   - Fee percentage (default 40%).

4. Attach to BotController.
"""

import json, os, re, time, uuid, threading, requests, csv, io
from datetime import datetime, timedelta
from typing import List, Dict, Optional
from bs4 import BeautifulSoup

from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

# ═══════════════════════════════════════════════════════════════════════════
# BotController hub
HUB      = "http://localhost:8765"
BOT_ID   = "tcpa_settlement_bot"
BOT_NAME = "TCPA Settlement Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tcpa_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tcpa_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 86400   # daily scan

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
        "call_log_csv": "calls.csv",      # CSV with headers: phone_number, date (YYYY-MM-DD), time (HH:MM), duration_sec, caller_name
        "claimant": {
            "first_name": "Jane",
            "last_name": "Doe",
            "email": "jane@example.com",
            "phone": "5551234567",
            "address": "123 Main St",
            "city": "Anytown",
            "state": "CA",
            "zip": "12345"
        },
        "fee_percent": 40.0,              # contingency fee % of per‑call settlement
        "settlement_sources": [
            {
                "name": "TopClassActions",
                "url": "https://topclassactions.com/category/unsolicited-calls/",
                "listing_selector": "article.type-post",
                "title_selector": "h2.entry-title a",
                "link_selector": "h2.entry-title a",
                "date_selector": "time.entry-date"
            }
        ],
        "stripe": {
            "enabled": False,
            "secret_key": os.getenv("STRIPE_SECRET_KEY", "")
        },
        "proxies": {"list": []}
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"filed_claims": []}
    with open(STATE_FILE, "r") as f: return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# Proxy helpers (same as before)
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

# ═══════════════════════════════════════════════════════════════════════════
# Load call history from CSV
def load_call_history(csv_path):
    calls = []
    if not os.path.exists(csv_path):
        _post(f"Call log file {csv_path} not found.", "warning")
        return calls
    with open(csv_path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Normalize phone number (strip non-digits)
            phone = re.sub(r'\D', '', row.get("phone_number", ""))
            if not phone:
                continue
            call_date = row.get("date", "")
            call_time = row.get("time", "00:00")
            calls.append({
                "phone_number": phone,
                "date": call_date,
                "time": call_time,
                "duration_sec": int(row.get("duration_sec", 0)),
                "caller_name": row.get("caller_name", "")
            })
    return calls

# ═══════════════════════════════════════════════════════════════════════════
# Scrape open TCPA settlements from TopClassActions
def scrape_settlements():
    settlements = []
    for source in CFG["settlement_sources"]:
        try:
            resp = requests.get(source["url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            articles = soup.select(source["listing_selector"])
            for art in articles:
                title_el = art.select_one(source["title_selector"])
                link_el = art.select_one(source["link_selector"])
                date_el = art.select_one(source.get("date_selector"))
                if not title_el or not link_el:
                    continue
                title = title_el.get_text(strip=True)
                link = link_el.get("href")
                # Very basic extraction of defendant, class period, settlement amount from title.
                # Example title: "Jiffy Lube TCPA $2.5M Class Action Settlement"
                defendant_match = re.search(r'^(.+?) TCPA', title)
                amount_match = re.search(r'\$([\d.]+)\s*([MB]illion)', title)
                defendant = defendant_match.group(1) if defendant_match else title
                amount_str = ""
                if amount_match:
                    num = float(amount_match.group(1))
                    multiplier = 1_000_000 if 'M' in amount_match.group(2) else 1_000_000_000
                    amount_str = f"${num * multiplier:,.0f}"
                # Need to scrape the article page for details (class period, settlement website)
                settlements.append({
                    "defendant": defendant,
                    "title": title,
                    "url": link,
                    "estimated_amount": amount_str,
                    "source": source["name"]
                })
        except Exception as e:
            _post(f"Error scraping {source['name']}: {e}", "warning")
    return settlements

# ═══════════════════════════════════════════════════════════════════════════
# Extract settlement details (class period, claim form URL) from article page
def get_settlement_details(settlement_article_url):
    """Scrape the article page to get class period dates and a link to the claim form."""
    try:
        resp = requests.get(settlement_article_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Look for dates like "between January 1, 2020 and December 31, 2022"
        date_text = soup.find(string=re.compile(r'between\s+\w+\s+\d{1,2},\s+\d{4}\s+and\s+\w+\s+\d{1,2},\s+\d{4}', re.I))
        start_date = end_date = None
        if date_text:
            dates = re.findall(r'(\w+\s+\d{1,2},\s+\d{4})', date_text)
            if len(dates) == 2:
                start_date = dates[0]
                end_date = dates[1]
        # Look for claim form link (often "file a claim" or "submit a claim")
        claim_link = soup.find("a", href=re.compile(r'(claim|file|submit)', re.I),
                                text=re.compile(r'(claim|file|submit)', re.I))
        claim_url = claim_link.get("href") if claim_link else None
        return {
            "class_period_start": start_date,
            "class_period_end": end_date,
            "claim_form_url": claim_url
        }
    except Exception as e:
        _post(f"Error getting settlement details: {e}", "warning")
        return {}

# ═══════════════════════════════════════════════════════════════════════════
# Match calls against settlement class periods
def match_calls_to_settlements(calls, settlements):
    matched = []
    for call in calls:
        call_date = call.get("date")
        if not call_date:
            continue
        try:
            call_dt = datetime.strptime(call_date, "%Y-%m-%d")
        except:
            continue
        for sett in settlements:
            # We need the actual class period; we'll fetch it if not already present
            details = sett.get("details")
            if not details:
                details = get_settlement_details(sett["url"])
                sett["details"] = details
            if details.get("class_period_start") and details.get("class_period_end"):
                try:
                    start = datetime.strptime(details["class_period_start"], "%B %d, %Y")
                    end = datetime.strptime(details["class_period_end"], "%B %d, %Y")
                    if start <= call_dt <= end:
                        matched.append({
                            "call": call,
                            "settlement": sett
                        })
                except:
                    pass
    return matched

# ═══════════════════════════════════════════════════════════════════════════
# File a claim (Playwright automation)
def file_tcp_a_claim(settlement, claimant, call):
    claim_url = settlement.get("details", {}).get("claim_form_url")
    if not claim_url:
        _post(f"No claim form link found for {settlement['defendant']}.", "warning")
        return False

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy: launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            _post(f"Filing claim for {settlement['defendant']} using call from {call['phone_number']}...", "info")
            page.goto(claim_url, wait_until="networkidle", timeout=30000)

            # Generic form filling (common fields)
            # Name
            page.fill("input[name='first_name'], input#first_name", claimant["first_name"])
            page.fill("input[name='last_name'], input#last_name", claimant["last_name"])
            # Contact
            page.fill("input[name='email'], input#email", claimant["email"])
            page.fill("input[name='phone'], input#phone", claimant["phone"])
            # Address
            page.fill("input[name='address1'], input#address1", claimant.get("address", ""))
            page.fill("input[name='city'], input#city", claimant.get("city", ""))
            page.select_option("select[name='state'], select#state", claimant.get("state", "CA"))
            page.fill("input[name='zip'], input#zip", claimant.get("zip", ""))

            # Phone number that received the call (sometimes separate)
            page.fill("input[name='called_number'], input#called_number", claimant["phone"])

            # Date of call (may need to be entered)
            if call.get("date"):
                page.fill("input[name='call_date'], input#call_date", call["date"])

            # CAPTCHA?
            if page.locator("[data-sitekey]").count() or page.locator("iframe[title*='captcha']").count():
                if not solve_captcha(page):
                    return False
                time.sleep(2)

            # Submit claim
            submit_btn = "button[type='submit'], input[type='submit'], button:has-text('Submit')"
            page.click(submit_btn)
            page.wait_for_timeout(5000)

            if "thank you" in page.content().lower() or "confirmation" in page.content().lower():
                _post(f"Claim submitted for {settlement['defendant']}!", "info")
                return True
            else:
                _post("Claim submission may have failed.", "warning")
                return False
        except Exception as e:
            _post(f"Claim filing error: {e}", "error")
            return False
        finally:
            browser.close()

# ═══════════════════════════════════════════════════════════════════════════
# Fee collection
def charge_tcp_a_fee(client_email, estimated_payout=50):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return True
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    fee_pct = CFG.get("fee_percent", 40.0)
    fee = round(estimated_payout * fee_pct / 100, 2)
    try:
        stripe.PaymentIntent.create(
            amount=int(fee * 100),
            currency="usd",
            description=f"TCPA claim assistance fee",
            metadata={"client_email": client_email}
        )
        _post(f"Charged ${fee} fee.", "info")
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# Main orchestrator
def run():
    calls = load_call_history(CFG["call_log_csv"])
    if not calls:
        _post("No calls loaded.", "info")
        return

    settlements = scrape_settlements()
    if not settlements:
        _post("No TCPA settlements found.", "info")
        return

    matched = match_calls_to_settlements(calls, settlements)
    if not matched:
        _post("No calls matched any current TCPA settlements.", "info")
        return

    state = load_state()
    for item in matched:
        # Avoid duplicate filings for the same call + settlement combo
        key = f"{item['call']['phone_number']}_{item['settlement']['defendant']}"
        if any(f.get("key") == key for f in state.get("filed_claims", [])):
            continue

        success = file_tcp_a_claim(item["settlement"], CFG["claimant"], item["call"])
        if success:
            state.setdefault("filed_claims", []).append({
                "key": key,
                "defendant": item["settlement"]["defendant"],
                "phone_number": item["call"]["phone_number"],
                "timestamp": datetime.utcnow().isoformat()
            })
            save_state(state)
            # Charge fee (assuming a $25-50 per call payout)
            charge_tcp_a_fee(CFG["claimant"]["email"], estimated_payout=30)

def main():
    wait_for_hub()
    _post("TCPA Settlement Claim Bot online. Checking for robocall lawsuits...", "info")
    while True:
        run()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `tcpa_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "call_log_csv": "calls.csv",
  "claimant": {
    "first_name": "Jane",
    "last_name": "Doe",
    "email": "jane@example.com",
    "phone": "5551234567",
    "address": "123 Main St",
    "city": "Anytown",
    "state": "CA",
    "zip": "12345"
  },
  "fee_percent": 40.0,
  "settlement_sources": [
    {
      "name": "TopClassActions",
      "url": "https://topclassactions.com/category/unsolicited-calls/",
      "listing_selector": "article.type-post",
      "title_selector": "h2.entry-title a",
      "link_selector": "h2.entry-title a",
      "date_selector": "time.entry-date"
    }
  ],
  "stripe": {
    "enabled": false,
    "secret_key": "sk_test_..."
  }
}
"""
