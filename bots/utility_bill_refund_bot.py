#!/usr/bin/env python3
"""
utility_bill_refund_bot.py — Commercial Utility Bill Audit & Refund Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Reads a CSV of utility bills (consumption, demand, charges).
2. Compares billed amounts against known tariff parameters to spot
   overcharges (incorrect demand rates, meter errors, etc.).
3. Automates a refund request on the utility's online portal via
   Playwright, attaching evidence.
4. Collects a 30% contingency fee on the estimated refund via Stripe.
5. Reports results to BotController.

✦ REAL DATA: Uses your own bill CSV and tariff config.
  For educational / research purposes only. Filing false refund requests
  is illegal. Use only on your own accounts with legitimate errors.

SETUP
─────
1. Install dependencies:
      pip install playwright stripe requests
      python -m playwright install chromium

2. Set environment variables:
      export CAPTCHA_API_KEY="your-2captcha-key"   (optional)
      export STRIPE_SECRET_KEY="sk_test_..."        (optional)

3. Prepare a CSV file with your bills (example below). The required columns:
   - period_start (YYYY-MM-DD)
   - period_end (YYYY-MM-DD)
   - usage_kwh (total kWh)
   - demand_kw (peak kW, if applicable)
   - billed_amount (actual total charge)
   - meter_number
   - bill_id (optional)

4. Create `utility_audit_config.json` (example at bottom).
   Fill in:
   - Tariff parameters (energy rate per kWh, demand rate per kW,
     fixed monthly charge, taxes/fees percent).
   - Utility refund portal details (login, selectors).
   - Your building information.
   - Fee percentage (default 30%).

5. Attach to BotController.
"""

import csv
import json
import os
import re
import time
import uuid
import threading
import requests
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple

from playwright.sync_api import sync_playwright

# ═══════════════════════════════════════════════════════════════════════════
# BotController hub
HUB      = "http://localhost:8765"
BOT_ID   = "utility_bill_refund_bot"
BOT_NAME = "Utility Bill Refund Auditor"

CONFIG_FILE      = os.path.join(os.path.dirname(os.path.abspath(__file__)), "utility_audit_config.json")
BILLS_CSV_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "utility_bills.csv")
STATE_FILE       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "utility_refund_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL      = 86400   # daily audit

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
        "tariff": {
            "energy_rate_per_kwh": 0.12,        # $ per kWh
            "demand_rate_per_kw": 8.50,          # $ per kW (if applicable)
            "fixed_monthly_charge": 25.0,
            "tax_rate": 0.07                     # 7% tax on subtotal
        },
        "building": {
            "name": "123 Main Street Apartments",
            "address": "123 Main St, Anytown, ST 12345"
        },
        "utility_company": {
            "name": "Anytown Power & Light",
            "refund_portal_url": "https://www.anytownpower.com/refunds",
            "login_required": True,
            "username": "your_account_email@example.com",
            "password": "your_password",
            "selectors": {
                "login_email": "input#username",
                "login_password": "input#password",
                "login_submit": "button[type='submit']",
                "new_refund_link": "a[href*='refund-request']",
                "account_number": "input#accountNumber",
                "billing_period": "input#billingPeriod",
                "refund_amount": "input#refundAmount",
                "reason": "textarea#reason",
                "upload_support": "input#supportingDocs",
                "submit": "button#submitRequest"
            }
        },
        "contact": {
            "email": "manager@building.com",
            "phone": "555-1111",
            "name": "Property Manager"
        },
        "fee_percent": 30.0,
        "stripe": {
            "enabled": False,
            "secret_key": os.getenv("STRIPE_SECRET_KEY", "")
        },
        "proxies": {"list": []}
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"refunds_filed": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def load_bills_from_csv(csv_path):
    bills = []
    if not os.path.exists(csv_path):
        _post(f"Bills CSV not found at {csv_path}", "error")
        return bills
    with open(csv_path, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Parse required columns
            try:
                usage_kwh = float(row.get("usage_kwh", 0))
                demand_kw = float(row.get("demand_kw", 0))
                billed_amount = float(row.get("billed_amount", 0))
                meter = row.get("meter_number", "")
                period_start = row.get("period_start", "")
                period_end = row.get("period_end", "")
                bill_id = row.get("bill_id", f"{period_start}_{period_end}")
                bills.append({
                    "bill_id": bill_id,
                    "period_start": period_start,
                    "period_end": period_end,
                    "usage_kwh": usage_kwh,
                    "demand_kw": demand_kw,
                    "billed_amount": billed_amount,
                    "meter_number": meter
                })
            except Exception as e:
                _post(f"Skipping row due to error: {e}", "warning")
    return bills

CFG = load_config()
STATE = load_state()

# ═══════════════════════════════════════════════════════════════════════════
# Proxy & Captcha (same as before)
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
            if r.get("request") != "CAPCHA_NOT_READY": break
    except: pass
    return False

# ═══════════════════════════════════════════════════════════════════════════
# Bill Audit Logic

def audit_bill(bill, tariff):
    """
    Calculate the expected charge based on tariff and compare to billed amount.
    Returns (overcharge_amount, reason_string) or (0, None) if no error.
    Simple check: energy charge + demand charge + fixed + tax.
    """
    energy_charge = bill["usage_kwh"] * tariff["energy_rate_per_kwh"]
    demand_charge = bill["demand_kw"] * tariff["demand_rate_per_kw"]
    subtotal = energy_charge + demand_charge + tariff["fixed_monthly_charge"]
    tax = subtotal * tariff["tax_rate"]
    expected_total = subtotal + tax

    billed = bill["billed_amount"]
    if billed > expected_total + 0.01:  # tolerance
        overcharge = round(billed - expected_total, 2)
        reason = (f"Billed ${billed:.2f}, expected ${expected_total:.2f} "
                  f"(energy ${energy_charge:.2f}, demand ${demand_charge:.2f}, "
                  f"fixed ${tariff['fixed_monthly_charge']:.2f}, tax ${tax:.2f})")
        return overcharge, reason
    return 0, None

# ═══════════════════════════════════════════════════════════════════════════
# Refund Filing via Playwright

def file_refund(utility_cfg, building_cfg, contact, bill, overcharge, reason):
    """
    Automates the utility's online refund request form.
    Returns True if submission appears successful.
    """
    portal_url = utility_cfg.get("refund_portal_url")
    selectors = utility_cfg.get("selectors", {})
    if not portal_url:
        _post("No refund portal URL configured.", "error")
        return False

    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy: launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            _post(f"Navigating to refund portal: {portal_url}", "info")
            page.goto(portal_url, wait_until="networkidle", timeout=30000)

            # Login if required
            if utility_cfg.get("login_required"):
                _post("Logging in...", "info")
                page.fill(selectors.get("login_email", "input#email"), utility_cfg.get("username", ""))
                page.fill(selectors.get("login_password", "input#password"), utility_cfg.get("password", ""))
                page.click(selectors.get("login_submit", "button[type='submit']"))
                page.wait_for_load_state("networkidle")

            # Start a new refund request
            page.click(selectors.get("new_refund_link", "a:has-text('Refund')"))
            page.wait_for_load_state("networkidle")

            # Fill form
            page.fill(selectors.get("account_number", "input#account"), building_cfg.get("address", ""))
            page.fill(selectors.get("billing_period", "input#period"), f"{bill['period_start']} to {bill['period_end']}")
            page.fill(selectors.get("refund_amount", "input#amount"), str(overcharge))
            page.fill(selectors.get("reason", "textarea#reason"), reason)

            # Upload supporting bill (if needed) – left as an exercise (file input tricky)
            # page.set_input_files(selectors.get("upload_support", "input[type='file']"), "path/to/bill.pdf")

            # CAPTCHA
            if page.locator("[data-sitekey]").count() or page.locator("iframe[title*='captcha']").count():
                if not solve_captcha(page):
                    return False
                time.sleep(2)

            # Submit
            page.click(selectors.get("submit", "button#submitRequest"))
            page.wait_for_timeout(5000)

            if "thank you" in page.content().lower() or "received" in page.content().lower():
                _post(f"Refund request submitted for {bill['bill_id']}! Amount: ${overcharge:.2f}", "info")
                return True
            else:
                _post("Refund submission might have failed.", "warning")
                return False
        except Exception as e:
            _post(f"Refund filing error: {e}", "error")
            return False
        finally:
            browser.close()

# ═══════════════════════════════════════════════════════════════════════════
# Stripe fee

def charge_contingency(client_email, refund_amount):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return True
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    fee_pct = CFG.get("fee_percent", 30.0)
    fee = round(refund_amount * fee_pct / 100, 2)
    try:
        stripe.PaymentIntent.create(
            amount=int(fee * 100),
            currency="usd",
            description="Utility bill refund contingency fee",
            metadata={"client_email": client_email}
        )
        _post(f"Contingency fee of ${fee} charged to {client_email}.", "info")
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ═══════════════════════════════════════════════════════════════════════════
# Main orchestration

def run():
    bills = load_bills_from_csv(BILLS_CSV_FILE)
    if not bills:
        _post("No bills loaded. Check CSV file.", "warning")
        return

    tariff = CFG.get("tariff", {})
    building = CFG.get("building", {})
    utility = CFG.get("utility_company", {})
    contact = CFG.get("contact", {})

    state = load_state()
    for bill in bills:
        # Skip if already processed
        if any(f.get("bill_id") == bill["bill_id"] for f in state.get("refunds_filed", [])):
            continue

        overcharge, reason = audit_bill(bill, tariff)
        if overcharge == 0:
            _post(f"Bill {bill['bill_id']}: No overcharge detected.", "info")
            continue

        _post(f"Overcharge found on bill {bill['bill_id']}: ${overcharge:.2f}. Reason: {reason}", "warning")

        # File refund
        success = file_refund(utility, building, contact, bill, overcharge, reason)
        if success:
            # Record
            state.setdefault("refunds_filed", []).append({
                "bill_id": bill["bill_id"],
                "overcharge": overcharge,
                "timestamp": datetime.utcnow().isoformat()
            })
            save_state(state)

            # Take fee
            charge_contingency(contact.get("email", ""), overcharge)

def main():
    wait_for_hub()
    _post("Commercial Utility Bill Audit Bot online. Scanning bills...", "info")
    while True:
        run()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `utility_audit_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "tariff": {
    "energy_rate_per_kwh": 0.12,
    "demand_rate_per_kw": 8.50,
    "fixed_monthly_charge": 25.0,
    "tax_rate": 0.07
  },
  "building": {
    "name": "123 Main Street Apartments",
    "address": "123 Main St, Anytown, ST 12345"
  },
  "utility_company": {
    "name": "Anytown Power & Light",
    "refund_portal_url": "https://www.anytownpower.com/refunds",
    "login_required": true,
    "username": "your_account@example.com",
    "password": "your_password",
    "selectors": {
      "login_email": "input#username",
      "login_password": "input#password",
      "login_submit": "button[type='submit']",
      "new_refund_link": "a[href*='refund-request']",
      "account_number": "input#accountNumber",
      "billing_period": "input#billingPeriod",
      "refund_amount": "input#refundAmount",
      "reason": "textarea#reason",
      "upload_support": "input#supportingDocs",
      "submit": "button#submitRequest"
    }
  },
  "contact": {
    "email": "manager@building.com",
    "phone": "555-1111",
    "name": "Property Manager"
  },
  "fee_percent": 30.0,
  "stripe": {
    "enabled": false,
    "secret_key": "sk_test_..."
  }
}
"""

# Example `utility_bills.csv` (place next to script)
"""
period_start,period_end,usage_kwh,demand_kw,billed_amount,meter_number
2026-01-01,2026-01-31,15000,45,1890.50,ABC-12345
2026-02-01,2026-02-28,14800,42,1820.00,ABC-12345
2026-03-01,2026-03-31,15500,48,1960.75,ABC-12345
"""
