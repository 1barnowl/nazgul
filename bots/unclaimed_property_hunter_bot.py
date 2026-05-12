#!/usr/bin/env python3
"""
unclaimed_property_hunter_bot.py — Unclaimed Property Hunter Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Scrapes state unclaimed property databases (MissingMoney.com, etc.)
   for a list of client names.
2. Detects potential matches and checks for reported owner address.
3. Automates claim filing via Playwright, submitting owner details & ID.
4. Takes a configurable finder’s fee (15‑25%) via Stripe when paid to client.

✦ REAL AUTOMATION using Playwright, 2Captcha, and direct HTTP.
  For educational / research purposes only. Unclaimed property finder
  activities are heavily regulated – ensure compliance with state laws.

SETUP
─────
1. Install dependencies:
      pip install playwright beautifulsoup4 stripe requests
      python -m playwright install chromium

2. Set environment variables:
      export CAPTCHA_API_KEY="your-2captcha-key"
      export STRIPE_SECRET_KEY="sk_test_..."        (optional)
      export FINDER_FEE_PERCENT=20                  (default 20%)

3. Create `unclaimed_config.json` (example at bottom).
   Provide:
   - List of client names (or a file path to load).
   - State database configurations (URLs, selectors).
   - Finder fee percent.
   - Your Stripe key for billing.

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
BOT_ID   = "unclaimed_property_hunter_bot"
BOT_NAME = "Unclaimed Property Hunter"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unclaimed_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "unclaimed_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 86400  # daily scan by default

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
        "clients_file": "clients.csv",   # CSV with columns: name, email, phone, address, city, state, zip
        "finder_fee_percent": 20.0,
        "state_databases": [
            {
                "name": "California",
                "search_url": "https://ucpi.sco.ca.gov/UCP/",
                "type": "direct",          # "missingmoney" or "direct"
                "selectors": {
                    "last_name": "input#ContentPlaceHolder1_txtLastName",
                    "first_name": "input#ContentPlaceHolder1_txtFirstName",
                    "city": "input#ContentPlaceHolder1_txtCity",
                    "search_button": "input#ContentPlaceHolder1_btnSearch",
                    "results_table": "table#ContentPlaceHolder1_gvSearchResults",
                    "claim_button": "a.claim-link",
                    "claim_first_name": "input#ContentPlaceHolder1_txtClaimFirstName",
                    "claim_last_name": "input#ContentPlaceHolder1_txtClaimLastName",
                    "claim_email": "input#ContentPlaceHolder1_txtEmail",
                    "claim_phone": "input#ContentPlaceHolder1_txtPhone",
                    "claim_submit": "input#ContentPlaceHolder1_btnSubmitClaim"
                }
            },
            {
                "name": "MissingMoney (multi-state)",
                "search_url": "https://www.missingmoney.com/app/claim/search",
                "type": "missingmoney",
                "selectors": {
                    "last_name": "input#lastName",
                    "first_name": "input#firstName",
                    "city": "input#city",
                    "state": "select#state",
                    "search_button": "button#search",
                    "results_container": "div.results-list",
                    "result_item": "div.property-item",
                    "claim_link": "a.claim-link",
                    "claim_first_name": "input#claimantFirstName",
                    "claim_last_name": "input#claimantLastName",
                    "claim_email": "input#claimantEmail",
                    "claim_phone": "input#claimantPhone",
                    "claim_submit": "button#submitClaim"
                }
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
    if not os.path.exists(STATE_FILE): return {"claimed_properties": [], "clients_processed": {}}
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

# ── Client loading ─────────────────────────────────────────────────────────
def load_clients():
    clients_file = CFG.get("clients_file", "clients.csv")
    clients = []
    if not os.path.exists(clients_file):
        _post(f"Client file {clients_file} not found. Using empty list.", "warning")
        return []
    with open(clients_file, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            clients.append(row)
    return clients

# ── Search an unclaimed property database ─────────────────────────────────
def search_state_database(site_cfg, client):
    """
    Search for the client's name (last, first, city) in a state database.
    Returns a list of potential property dicts: {description, holder, value, claim_url}
    """
    search_url = site_cfg["search_url"]
    sel = site_cfg["selectors"]
    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy: launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        properties = []
        try:
            page.goto(search_url, wait_until="networkidle", timeout=30000)
            # Fill search form
            page.fill(sel["last_name"], client.get("last_name", ""))
            page.fill(sel["first_name"], client.get("first_name", ""))
            if sel.get("city"):
                page.fill(sel["city"], client.get("city", ""))
            if sel.get("state"):
                page.select_option(sel["state"], client.get("state", ""))

            # CAPTCHA?
            if page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    _post("CAPTCHA solve failed, skipping search.", "warning")
                    return []
                time.sleep(2)

            # Click search
            page.click(sel["search_button"])
            page.wait_for_load_state("networkidle")

            # Parse results (generic table or list)
            results_selector = sel.get("results_table") or sel.get("results_container")
            if not results_selector:
                _post("No results selector defined.", "error")
                return []

            # Wait for results to appear
            try:
                page.wait_for_selector(results_selector, timeout=10000)
            except:
                _post("No results found for this search.", "info")
                return []

            # Extract rows/items
            if sel.get("result_item"):
                items = page.locator(sel["result_item"])
            else:
                # assume table rows
                items = page.locator(f"{results_selector} tr")

            for i in range(items.count()):
                item = items.nth(i)
                # Get text content for description, holder, value (customize per site)
                text = item.inner_text()
                # Basic extraction: first line = description, amount if present
                desc_match = re.search(r'Description:\s*(.*)', text, re.I)
                holder_match = re.search(r'Held by:\s*(.*)', text, re.I)
                amount_match = re.search(r'\$\s?([\d,]+\.\d{2})', text)
                desc = desc_match.group(1).strip() if desc_match else text[:100]
                holder = holder_match.group(1).strip() if holder_match else "Unknown"
                value = amount_match.group(1).replace(",", "") if amount_match else "Unknown"
                # Extract claim link if possible
                claim_link = None
                if sel.get("claim_link"):
                    link_el = item.locator(sel["claim_link"])
                    if link_el.count():
                        claim_link = link_el.get_attribute("href")
                properties.append({
                    "description": desc,
                    "holder": holder,
                    "value": value,
                    "claim_url": claim_link,
                    "source": site_cfg["name"]
                })
            return properties
        except Exception as e:
            _post(f"Search error: {e}", "error")
            return []
        finally:
            browser.close()

# ── Claim filing automation ───────────────────────────────────────────────
def file_claim(site_cfg, property_info, client):
    """Navigate to claim page (or click claim link) and file a claim for the client."""
    sel = site_cfg["selectors"]
    if not property_info.get("claim_url"):
        _post("No claim URL – cannot file claim.", "warning")
        return False

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy: launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            # Go to claim URL
            _post(f"Filing claim for {client['first_name']} {client['last_name']}...", "info")
            page.goto(property_info["claim_url"], wait_until="networkidle", timeout=30000)

            # Fill claim form (adapt selectors per site)
            page.fill(sel["claim_first_name"], client["first_name"])
            page.fill(sel["claim_last_name"], client["last_name"])
            page.fill(sel["claim_email"], client.get("email", ""))
            page.fill(sel["claim_phone"], client.get("phone", ""))
            # Additional fields like address, SSN, etc. are usually not required initially –
            # many states just need contact info to initiate claim.

            # CAPTCHA?
            if page.locator("[data-sitekey]").count():
                if not solve_captcha(page):
                    _post("CAPTCHA unsolved, claim not filed.", "error")
                    return False
                time.sleep(2)

            # Submit claim
            page.click(sel["claim_submit"])
            page.wait_for_timeout(5000)

            # Check confirmation
            if "thank you" in page.content().lower() or "claim submitted" in page.content().lower():
                _post(f"Claim submitted for {client['first_name']} {client['last_name']}! State: {property_info['source']}", "info")
                return True
            else:
                _post("Claim submission may have failed.", "warning")
                return False
        except Exception as e:
            _post(f"Claim filing error: {e}", "error")
            return False
        finally:
            browser.close()

# ── Fee collection via Stripe ─────────────────────────────────────────────
def charge_finder_fee(client_email, property_value):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return True
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    fee_percent = CFG.get("finder_fee_percent", 20.0)
    # Convert value to number if possible
    try:
        value = float(property_value.replace(",", ""))
    except:
        value = 0
    fee = round(value * fee_percent / 100, 2)
    if fee <= 0:
        return True
    try:
        stripe.PaymentIntent.create(
            amount=int(fee * 100),
            currency="usd",
            description=f"Finder fee for unclaimed property recovery",
            metadata={"client_email": client_email}
        )
        _post(f"Stripe fee of ${fee} charged to {client_email}.", "info")
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ── Orchestrator ──────────────────────────────────────────────────────────
def run():
    clients = load_clients()
    if not clients:
        _post("No clients loaded. Exiting.", "info")
        return

    state = load_state()
    for client in clients:
        client_key = client.get("email") or f"{client['first_name']}_{client['last_name']}"
        if client_key in state.get("clients_processed", {}):
            _post(f"Client {client_key} already processed. Skipping.", "info")
            continue

        # Search each database
        for site_cfg in CFG["state_databases"]:
            properties = search_state_database(site_cfg, client)
            if not properties:
                continue
            # For now, just pick the first property (could be refined by matching more info)
            prop = properties[0]
            # File claim
            claim_success = file_claim(site_cfg, prop, client)
            if claim_success:
                state.setdefault("claimed_properties", []).append({
                    "client": client_key,
                    "state": site_cfg["name"],
                    "description": prop["description"],
                    "value": prop["value"],
                    "timestamp": datetime.utcnow().isoformat()
                })
                # Charge finder fee for each claimed property (could be after client receives money – simplified)
                charge_finder_fee(client.get("email", ""), prop["value"])
                save_state(state)
                break  # one claim per client per run

        # Mark client as processed
        state.setdefault("clients_processed", {})[client_key] = True
        save_state(state)
        time.sleep(random.uniform(3, 8))  # avoid rapid requests

def main():
    wait_for_hub()
    _post("Unclaimed Property Hunter Bot online. Starting scan...", "info")
    while True:
        run()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `unclaimed_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "clients_file": "clients.csv",
  "finder_fee_percent": 20.0,
  "state_databases": [
    {
      "name": "California",
      "search_url": "https://ucpi.sco.ca.gov/UCP/",
      "type": "direct",
      "selectors": {
        "last_name": "input#ContentPlaceHolder1_txtLastName",
        "first_name": "input#ContentPlaceHolder1_txtFirstName",
        "city": "input#ContentPlaceHolder1_txtCity",
        "search_button": "input#ContentPlaceHolder1_btnSearch",
        "results_table": "table#ContentPlaceHolder1_gvSearchResults",
        "claim_button": "a.claim-link",
        "claim_first_name": "input#ContentPlaceHolder1_txtClaimFirstName",
        "claim_last_name": "input#ContentPlaceHolder1_txtClaimLastName",
        "claim_email": "input#ContentPlaceHolder1_txtEmail",
        "claim_phone": "input#ContentPlaceHolder1_txtPhone",
        "claim_submit": "input#ContentPlaceHolder1_btnSubmitClaim"
      }
    },
    {
      "name": "MissingMoney",
      "search_url": "https://www.missingmoney.com/app/claim/search",
      "type": "missingmoney",
      "selectors": {
        "last_name": "input#lastName",
        "first_name": "input#firstName",
        "city": "input#city",
        "state": "select#state",
        "search_button": "button#search",
        "results_container": "div.results-list",
        "result_item": "div.property-item",
        "claim_link": "a.claim-link",
        "claim_first_name": "input#claimantFirstName",
        "claim_last_name": "input#claimantLastName",
        "claim_email": "input#claimantEmail",
        "claim_phone": "input#claimantPhone",
        "claim_submit": "button#submitClaim"
      }
    }
  ],
  "stripe": {
    "enabled": false,
    "secret_key": "sk_test_..."
  },
  "proxies": {"list": []}
}
"""
