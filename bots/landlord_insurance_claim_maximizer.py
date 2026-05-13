#!/usr/bin/env python3
"""
landlord_insurance_claim_maximizer.py — Insurance Claim Maximizer Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Parses policy fine print (PDF or config) to discover every coverage item,
   sub‑limit, and endorsement.
2. Cross‑references with a property damage report to identify under‑claimed
   losses.
3. Computes the maximum recoverable amount and calculates the uplift over the
   initial claim.
4. Automates submission of an enhanced claim via the insurer’s online portal
   (Playwright), filling in detailed damage descriptions, attaching documents,
   and requesting the full amount.
5. Charges 25% of the uplift via Stripe (contingency fee) once the adjusted
   claim is submitted.

✦ REAL‑WORLD AUTOMATION: Playwright, 2Captcha, Stripe, PyPDF2, BeautifulSoup.
  For educational and research purposes only. Submitting fraudulent claims is
  a serious criminal offense. Use only on test accounts or with explicit
  authorization on your own claims.

SETUP
─────
1. Install dependencies:
      pip install playwright PyPDF2 stripe requests beautifulsoup4
      python -m playwright install chromium

2. Place the policy document as a PDF (e.g., "policy.pdf") next to this script,
   or provide a JSON config with coverage details (example below).

3. Create a damage report JSON file (example below) listing damaged items and
   the landlord’s initial estimate.

4. Set environment variables:
      export CAPTCHA_API_KEY="your‑2captcha‑key"   (optional)
      export STRIPE_SECRET_KEY="sk_test_..."        (optional)

5. Create `claim_config.json` with insurer login details, portal selectors,
   and your personal info (example at bottom).

6. Attach to BotController.
"""

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

try:
    import PyPDF2
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# ═══════════════════════════════════════════════════════════════════════════
# BotController hub
HUB = "http://localhost:8765"
BOT_ID = "landlord_insurance_claim_maximizer"
BOT_NAME = "Insurance Claim Maximizer"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claim_config.json")
POLICY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy.pdf")
DAMAGE_REPORT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "damage_report.json")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claim_state.json")

HEARTBEAT_INTERVAL = 30
SCAN_INTERVAL = 3600  # hourly

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
# Config & Support Files

def load_json_file(filename, default=None):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            return json.load(f)
    return default if default is not None else {}

def load_config():
    default = {
        "insurer": {
            "name": "Example Insurance Co.",
            "portal_url": "https://claims.example-insurance.com",
            "login_required": True,
            "username": "your_agent_email@example.com",
            "password": "your_password",
            "selectors": {
                "login_email": "input#email",
                "login_password": "input#password",
                "login_submit": "button[type='submit']",
                "new_claim_link": "a.new-claim",
                "claim_type": "select#claimType",
                "date_of_loss": "input#dateOfLoss",
                "description": "textarea#description",
                "amount_claimed": "input#amountClaimed",
                "upload_docs": "input#fileUpload",
                "submit_claim": "button#submitClaim"
            }
        },
        "agent": {
            "name": "Jane Adjuster",
            "email": "jane@example.com",
            "phone": "555-4321"
        },
        "fee_percent": 25.0,
        "stripe": {
            "enabled": False,
            "secret_key": os.getenv("STRIPE_SECRET_KEY", "")
        }
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_policy_data():
    """Extract policy details from a PDF or use a separate config if available."""
    # First try a JSON overrides file
    policy_json_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "policy_details.json")
    if os.path.exists(policy_json_file):
        with open(policy_json_file, "r") as f:
            return json.load(f)

    # Otherwise parse the PDF for key coverage limits (simplified)
    if not os.path.exists(POLICY_FILE) or not HAS_PYPDF:
        return None

    policy_text = ""
    with open(POLICY_FILE, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        for page in reader.pages:
            policy_text += page.extract_text() + "\n"

    # Very basic extraction – in reality you’d need a robust parser.
    # For demo, we extract Coverage A (Dwelling), Coverage B (Other Structures),
    # Coverage C (Personal Property), Coverage D (Loss of Use), deductibles,
    # and any endorsements like "Water Backup" or "Building Code Upgrade".
    limits = {}
    # Look for pattern "Coverage A – Dwelling: $500,000"
    cov_a = re.search(r'Coverage\s*A[:\-]\s*Dwelling[:\-]?\s*\$?([\d,]+)', policy_text, re.I)
    if cov_a: limits["dwelling"] = float(cov_a.group(1).replace(",", ""))
    cov_b = re.search(r'Coverage\s*B[:\-]\s*Other\s*Structures[:\-]?\s*\$?([\d,]+)', policy_text, re.I)
    if cov_b: limits["other_structures"] = float(cov_b.group(1).replace(",", ""))
    cov_c = re.search(r'Coverage\s*C[:\-]\s*Personal\s*Property[:\-]?\s*\$?([\d,]+)', policy_text, re.I)
    if cov_c: limits["personal_property"] = float(cov_c.group(1).replace(",", ""))
    cov_d = re.search(r'Coverage\s*D[:\-]\s*Loss\s*of\s*Use[:\-]?\s*\$?([\d,]+)', policy_text, re.I)
    if cov_d: limits["loss_of_use"] = float(cov_d.group(1).replace(",", ""))
    deductible = re.search(r'Deductible[:\-]?\s*\$?([\d,]+)', policy_text, re.I)
    if deductible: limits["deductible"] = float(deductible.group(1).replace(",", ""))

    # Endorsements
    endorsements = []
    if "water backup" in policy_text.lower() or "sump overflow" in policy_text.lower():
        amt = re.search(r'Water\s*Back[:\-]?\s*up[:\-]?\s*\$?([\d,]+)', policy_text, re.I)
        endorsements.append({
            "type": "water_backup",
            "limit": float(amt.group(1).replace(",", "")) if amt else 5000
        })
    if "building code" in policy_text.lower() or "ordinance or law" in policy_text.lower():
        amt = re.search(r'Ordinance\s*or\s*Law[:\-]?\s*\$?([\d,]+)', policy_text, re.I)
        endorsements.append({
            "type": "building_code_upgrade",
            "limit": float(amt.group(1).replace(",", "")) if amt else 10000
        })
    if "mold" in policy_text.lower():
        amt = re.search(r'Mold[:\-]?\s*\$?([\d,]+)', policy_text, re.I)
        endorsements.append({
            "type": "mold_remediation",
            "limit": float(amt.group(1).replace(",", "")) if amt else 10000
        })

    return {
        "limits": limits,
        "endorsements": endorsements
    }

def load_damage_report():
    """Load the initial claim estimate from the landlord."""
    if os.path.exists(DAMAGE_REPORT_FILE):
        with open(DAMAGE_REPORT_FILE, "r") as f:
            return json.load(f)
    # If not present, create an example
    return {
        "date_of_loss": "2026-05-01",
        "description": "Water damage from burst pipe in kitchen",
        "items": [
            {"description": "Repair kitchen ceiling drywall", "category": "dwelling", "estimated_cost": 1200},
            {"description": "Replace kitchen cabinet bottoms", "category": "other_structures", "estimated_cost": 800},
            {"description": "Refinish hardwood floor (kitchen area)", "category": "dwelling", "estimated_cost": 1500}
        ],
        "initial_total_claimed": 3500.0,
        "landlord_email": "landlord@example.com",
        "landlord_name": "John Landlord"
    }

def load_state():
    return load_json_file(STATE_FILE, {"processed_claims": []})

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()
DAMAGE_REPORT = load_damage_report()
POLICY = load_policy_data()

# If no policy data, we can't proceed
if not POLICY:
    _post("No policy details found. Please provide policy.pdf or policy_details.json.", "error")
    exit(1)

# ── Proxy & Captcha (standard) ────────────────────────────────────────────
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

# ── Claim Maximization Algorithm ──────────────────────────────────────────
def maximize_claim(policy_data, damage_report):
    """
    Apply policy fine print to find every entitlement.
    Returns (new_total, uplift, details_explanation)
    """
    limits = policy_data["limits"]
    endorsements = policy_data.get("endorsements", [])
    deductible = limits.get("deductible", 0)

    # Categorize items and apply coverage limits per category
    category_totals = {}
    for item in damage_report["items"]:
        cat = item.get("category", "dwelling")
        category_totals[cat] = category_totals.get(cat, 0) + item["estimated_cost"]

    # Check policy limits for each category
    coverage = {
        "dwelling": limits.get("dwelling", 0),
        "other_structures": limits.get("other_structures", 0),
        "personal_property": limits.get("personal_property", 0),
        "loss_of_use": limits.get("loss_of_use", 0)
    }

    adjusted_items = []
    for item in damage_report["items"]:
        cat = item["category"]
        limit = coverage.get(cat, 0)
        # If the item cost exceeds the category limit, max it out at the limit
        allowed = min(item["estimated_cost"], limit - (category_totals[cat] - item["estimated_cost"]))
        adjusted_items.append(allowed)

    # Add endorsements that may trigger based on damage description
    desc = damage_report.get("description", "").lower()
    additional = 0
    notes = []
    for endorsement in endorsements:
        etype = endorsement["type"]
        # Simple keyword matching
        if etype == "water_backup" and ("water" in desc or "sewer" in desc or "backup" in desc):
            additional += endorsement["limit"]
            notes.append(f"Water Backup endorsement: +${endorsement['limit']:.2f}")
        if etype == "building_code_upgrade" and ("code" in desc or "upgrade" in desc):
            additional += endorsement["limit"]
            notes.append(f"Building Code Upgrade: +${endorsement['limit']:.2f}")
        if etype == "mold_remediation" and ("mold" in desc or "water" in desc):
            additional += endorsement["limit"]
            notes.append(f"Mold Remediation: +${endorsement['limit']:.2f}")

    # Also consider Loss of Use if the damage makes the property uninhabitable
    if "uninhabitable" in desc or "loss of use" in desc:
        additional += coverage.get("loss_of_use", 0)
        notes.append(f"Loss of Use coverage: +${coverage.get('loss_of_use', 0):.2f}")

    # Compute total claimable (after deductible)
    initial_direct = sum(item["estimated_cost"] for item in damage_report["items"])
    direct_after_deductible = max(0, initial_direct - deductible)
    total_claimable = direct_after_deductible + additional

    # Uplift over original claimed amount
    original_claimed = damage_report.get("initial_total_claimed", initial_direct)
    uplift = max(0, total_claimable - original_claimed)

    return total_claimable, uplift, notes

# ── Stripe fee collection ─────────────────────────────────────────────────
def charge_fee(client_email, uplift_amount):
    if not CFG["stripe"]["enabled"] or not CFG["stripe"]["secret_key"]:
        return True
    import stripe
    stripe.api_key = CFG["stripe"]["secret_key"]
    fee_pct = CFG.get("fee_percent", 25.0)
    fee = round(uplift_amount * fee_pct / 100, 2)
    if fee <= 0: return True
    try:
        stripe.PaymentIntent.create(
            amount=int(fee * 100),
            currency="usd",
            description="Insurance claim uplift contingency fee",
            metadata={"client_email": client_email}
        )
        _post(f"Invoiced {client_email} ${fee} (25% of ${uplift_amount:.2f} uplift).", "info")
        return True
    except Exception as e:
        _post(f"Stripe error: {e}", "error")
        return False

# ── Automated claim filing via insurer portal (Playwright) ────────────────
def file_enhanced_claim(insurer_cfg, damage_report, total_claimed, uplift, notes):
    """
    Opens the insurer's online claim portal, fills in the claim form,
    attaches any evidence, and submits.
    """
    portal_url = insurer_cfg.get("portal_url")
    selectors = insurer_cfg.get("selectors", {})
    agent = CFG.get("agent", {})
    proxy = next_proxy()
    launch_opts = {"headless": False}
    if proxy: launch_opts["proxy"] = {"server": proxy}

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_opts)
        page = browser.new_page()
        try:
            _post(f"Logging into {insurer_cfg['name']} claims portal...", "info")
            page.goto(portal_url, wait_until="networkidle", timeout=30000)
            if insurer_cfg.get("login_required"):
                page.fill(selectors["login_email"], insurer_cfg.get("username", ""))
                page.fill(selectors["login_password"], insurer_cfg.get("password", ""))
                page.click(selectors["login_submit"])
                page.wait_for_load_state("networkidle")
                # Handle 2FA if any (skip)
                if "verify" in page.url.lower() or "code" in page.content().lower():
                    _post("2FA required – cannot proceed. Aborting.", "error")
                    return False

            # Start a new claim
            page.click(selectors.get("new_claim_link", "a:has-text('New Claim')"))
            page.wait_for_load_state("networkidle")

            # Fill claim details
            page.select_option(selectors.get("claim_type", "select#claimType"), "Property Damage")
            page.fill(selectors.get("date_of_loss", "input#dateOfLoss"), damage_report.get("date_of_loss", ""))
            description = f"{damage_report['description']}\n\nAdditional per policy review: {', '.join(notes)}"
            page.fill(selectors.get("description", "textarea#description"), description)
            page.fill(selectors.get("amount_claimed", "input#amountClaimed"), str(total_claimed))
            # Upload documents if needed (not implemented)

            # Submit
            page.click(selectors.get("submit_claim", "button#submitClaim"))
            page.wait_for_timeout(5000)

            if "thank you" in page.content().lower() or "claim received" in page.content().lower():
                _post(f"Enhanced claim submitted! Total claimed: ${total_claimed:.2f}. Uplift: ${uplift:.2f}", "info")
                return True
            else:
                _post("Claim submission may have failed.", "warning")
                return False
        except Exception as e:
            _post(f"Claim filing error: {e}", "error")
            return False
        finally:
            browser.close()

# ── Main orchestrator ──────────────────────────────────────────────────────
def run():
    global STATE, DAMAGE_REPORT, POLICY
    damage = DAMAGE_REPORT
    state = STATE
    # Check if we already processed this claim (by date and email)
    claim_key = f"{damage.get('date_of_loss')}_{damage.get('landlord_email')}"
    if any(c.get("key") == claim_key for c in state.get("processed_claims", [])):
        _post("Claim already maximized for this loss. Skipping.", "info")
        return

    total_claimable, uplift, notes = maximize_claim(POLICY, damage)
    if uplift == 0:
        _post("No uplift found. Claim is already maximized.", "info")
        return

    _post(f"Uplift opportunity: ${uplift:.2f} (from ${damage.get('initial_total_claimed', 0):.2f} to ${total_claimable:.2f}). Filing enhanced claim...", "warning")

    # File claim on insurer portal
    insurer = CFG.get("insurer", {})
    success = file_enhanced_claim(insurer, damage, total_claimable, uplift, notes)
    if success:
        # Record in state
        state.setdefault("processed_claims", []).append({
            "key": claim_key,
            "date_of_loss": damage["date_of_loss"],
            "landlord_email": damage["landlord_email"],
            "original_claimed": damage.get("initial_total_claimed", 0),
            "enhanced_claimed": total_claimable,
            "uplift": uplift,
            "timestamp": datetime.utcnow().isoformat()
        })
        save_state(state)
        # Charge the contingency fee
        charge_fee(damage.get("landlord_email", ""), uplift)

def main():
    wait_for_hub()
    _post("Landlord Insurance Claim Maximizer Bot online.", "info")
    while True:
        # Reload config & damage report in case updated
        global CFG, DAMAGE_REPORT, POLICY
        CFG = load_config()
        DAMAGE_REPORT = load_damage_report()
        POLICY = load_policy_data()
        run()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════
# Example `claim_config.json`
# ═══════════════════════════════════════════════════════════════════════════
"""
{
  "insurer": {
    "name": "Example Insurance Co.",
    "portal_url": "https://claims.example-insurance.com",
    "login_required": true,
    "username": "agent@example.com",
    "password": "secret",
    "selectors": {
      "login_email": "input#email",
      "login_password": "input#password",
      "login_submit": "button[type='submit']",
      "new_claim_link": "a.new-claim",
      "claim_type": "select#claimType",
      "date_of_loss": "input#dateOfLoss",
      "description": "textarea#description",
      "amount_claimed": "input#amountClaimed",
      "submit_claim": "button#submitClaim"
    }
  },
  "agent": {
    "name": "Jane Adjuster",
    "email": "jane@example.com",
    "phone": "555-4321"
  },
  "fee_percent": 25,
  "stripe": {
    "enabled": false,
    "secret_key": "sk_test_..."
  }
}
"""

# Example `damage_report.json`
"""
{
  "date_of_loss": "2026-05-01",
  "description": "Water damage from burst pipe in kitchen",
  "items": [
    {"description": "Repair kitchen ceiling drywall", "category": "dwelling", "estimated_cost": 1200},
    {"description": "Replace kitchen cabinet bottoms", "category": "other_structures", "estimated_cost": 800},
    {"description": "Refinish hardwood floor (kitchen area)", "category": "dwelling", "estimated_cost": 1500}
  ],
  "initial_total_claimed": 3500.0,
  "landlord_email": "landlord@example.com",
  "landlord_name": "John Landlord"
}
"""

# Example `policy_details.json` (alternative to PDF)
"""
{
  "limits": {
    "dwelling": 500000,
    "other_structures": 50000,
    "personal_property": 250000,
    "loss_of_use": 100000,
    "deductible": 1000
  },
  "endorsements": [
    {"type": "water_backup", "limit": 10000},
    {"type": "building_code_upgrade", "limit": 5000}
  ]
}
"""
