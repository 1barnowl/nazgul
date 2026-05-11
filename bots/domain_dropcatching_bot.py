#!/usr/bin/env python3
"""
domain_dropcatching_bot.py — Domain Drop‑Catching & Auction Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Fetches expiring domains from ExpiredDomains.net (free tier).
2. Filters by user‑defined quality metrics.
3. Checks backorder status on SnapNames, NameJet, DropCatch, GoDaddy.
4. Places backorders via API or browser (Playwright).
5. Monitors post‑catch auctions for domains you won, and auto‑bids.

✦ This is a RESEARCH TOOL for studying domain drop‑catch mechanics.
✦ Actual drop‑catching at the registry level requires Registrar accreditation
  and high‑speed registry connections – this bot leverages existing backorder
  services, not raw registry polling.

SETUP
─────
1. Install dependencies:
      pip install requests playwright beautifulsoup4
      python -m playwright install chromium

2. Get a free API key from https://member.expireddomains.net/api/
   Export:  EXPIREDDOMAINS_API_KEY="your‑key"

3. (Optional) For SnapNames / NameJet backorder execution:
   Export:  SNAPNAMES_USERNAME, SNAPNAMES_PASSWORD
            NAMEJET_USERNAME, NAMEJET_PASSWORD

4. (Optional) For GoDaddy backorder via browser:
   Ensure Playwright is installed.

5. Create `domain_dropcatch_config.json` (example at bottom).

6. Attach to BotController.
"""

import json
import os
import re
import time
import threading
import requests
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from bs4 import BeautifulSoup

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "domain_dropcatching_bot"
BOT_NAME = "Domain Drop‑Catcher"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "domain_dropcatch_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "domain_dropcatch_state.json")

SCAN_INTERVAL      = 3600   # 1 hour between new expiring domain scans
AUCTION_CHECK_INTERVAL = 900  # 15 minutes
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

# ── Configuration ──────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        default = {
            "expired_domains": {
                "api_key": os.getenv("EXPIREDDOMAINS_API_KEY", ""),
                "tld": "com",
                "min_domain_length": 3,
                "max_domain_length": 20,
                "max_dashes": 0,
                "min_moz_da": 15,
                "min_traffic": 100,
                "keywords": []   # e.g. ["crypto", "finance"]
            },
            "backorder_services": {
                "snapnames": {
                    "enabled": True,
                    "username": os.getenv("SNAPNAMES_USERNAME", ""),
                    "password": os.getenv("SNAPNAMES_PASSWORD", "")
                },
                "namejet": {
                    "enabled": True,
                    "username": os.getenv("NAMEJET_USERNAME", ""),
                    "password": os.getenv("NAMEJET_PASSWORD", "")
                },
                "dropcatch": {
                    "enabled": True,   # no API, uses browser
                    "max_backorder_attempts": 0  # how many backorders already before we skip
                },
                "godaddy": {
                    "enabled": True,   # browser only
                    "max_price": 20.0  # max backorder fee
                }
            },
            "auction": {
                "auto_bid": False,
                "max_bid": 100.0
            }
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

CFG = load_config()

# ── State file ─────────────────────────────────────────────────────────────────
def load_state():
    if not os.path.exists(STATE_FILE):
        return {"backordered": {}, "won_domains": {}}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── ExpiredDomains.net API ─────────────────────────────────────────────────────
def fetch_expiring_domains():
    api_key = CFG["expired_domains"].get("api_key", "").strip()
    if not api_key:
        _post("No ExpiredDomains API key set. Get one at member.expireddomains.net/api/", "error")
        return []

    params = {
        "key": api_key,
        "tld": CFG["expired_domains"]["tld"],
        "mode": "expiring",       # domains expiring soon
        "results": 100,
        "columns": "domain,mo_da,backlinks,traffic,length,dropdate",
        # Filtering is limited in free API; we'll filter after retrieval
    }
    try:
        resp = requests.get("https://member.expireddomains.net/api/", params=params, timeout=30)
        if resp.status_code != 200:
            _post(f"ExpiredDomains API returned {resp.status_code}: {resp.text[:200]}", "error")
            return []
        data = resp.json()
        # data structure: { "domains": [ {...}, ... ] }
        return data.get("domains", [])
    except Exception as e:
        _post(f"ExpiredDomains API error: {e}", "error")
        return []

def filter_domains(domains):
    cfg = CFG["expired_domains"]
    filtered = []
    for d in domains:
        name = d.get("domain", "")
        if not name:
            continue
        parts = name.split(".")
        if len(parts) < 2:
            continue
        sld = parts[0]
        dash_count = sld.count("-")
        if dash_count > cfg["max_dashes"]:
            continue
        length = len(sld)
        if length < cfg["min_domain_length"] or length > cfg["max_domain_length"]:
            continue

        moz_da = int(d.get("mo_da", 0) or 0)
        if moz_da < cfg["min_moz_da"]:
            continue

        traffic = int(d.get("traffic", 0) or 0)
        if traffic < cfg["min_traffic"]:
            continue

        # Keyword matching (simple substring)
        keywords = cfg.get("keywords", [])
        if keywords:
            domain_lower = sld.lower()
            if not any(kw.lower() in domain_lower for kw in keywords):
                continue

        filtered.append({
            "domain": name,
            "dropdate": d.get("dropdate", ""),
            "moz_da": moz_da,
            "backlinks": int(d.get("backlinks", 0) or 0),
            "traffic": traffic,
        })
    return filtered

# ── Backorder status checkers ─────────────────────────────────────────────────
def check_snapnames_status(domain):
    """Scrape SnapNames public backorder page for number of backorders."""
    try:
        url = f"https://www.snapnames.com/domain/{domain}/backorder"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Look for "Backorders: X" or similar
            text = soup.get_text()
            match = re.search(r'(\d+)\s*backorders?', text, re.IGNORECASE)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None

def check_namejet_status(domain):
    """Scrape NameJet public page."""
    try:
        url = f"https://www.namejet.com/pages/auctions/standardaucdetails.aspx?domainname={domain}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Find "Number of Bids" or "Backorders"
            text = soup.get_text()
            match = re.search(r'(\d+)\s*bids?', text, re.IGNORECASE)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None

def check_dropcatch_backorders(domain):
    """Scrape DropCatch public page for backorder count."""
    try:
        url = f"https://www.dropcatch.com/domain/{domain}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # They usually display "Backorders: XX" in a span
            elem = soup.find("span", class_="backorder-count")
            if elem:
                text = elem.get_text(strip=True)
                nums = re.findall(r'\d+', text)
                if nums:
                    return int(nums[0])
            # Fallback: whole page text
            text = soup.get_text()
            match = re.search(r'(\d+)\s*backorders?', text, re.IGNORECASE)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return 0  # assume 0 if can't read

def check_godaddy_auction_status(domain):
    """Scrape GoDaddy Auctions public listing."""
    try:
        url = f"https://auctions.godaddy.com/trpItemListing.aspx?domain={domain}"
        resp = requests.get(url, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Look for current bid or "Bids:"
            text = soup.get_text()
            match = re.search(r'(\d+)\s*bids?', text, re.IGNORECASE)
            if match:
                return int(match.group(1))
    except Exception:
        pass
    return None

def get_backorder_status(domain):
    """Return dict of service -> number of existing backorders/bids."""
    status = {}
    if CFG["backorder_services"]["snapnames"]["enabled"]:
        cnt = check_snapnames_status(domain)
        if cnt is not None:
            status["snapnames"] = cnt
    if CFG["backorder_services"]["namejet"]["enabled"]:
        cnt = check_namejet_status(domain)
        if cnt is not None:
            status["namejet"] = cnt
    if CFG["backorder_services"]["dropcatch"]["enabled"]:
        status["dropcatch"] = check_dropcatch_backorders(domain)
    return status

# ── Backorder executors ────────────────────────────────────────────────────────
def place_snapnames_backorder(domain, username, password):
    """Use SnapNames API (unofficial) via requests; may require session handling."""
    # SnapNames does not have a public API. Use browser automation instead.
    if HAS_PLAYWRIGHT:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto("https://www.snapnames.com/login.jsp", timeout=20000)
                page.fill("input[name='username']", username)
                page.fill("input[name='password']", password)
                page.click("input[type='submit']")
                page.wait_for_load_state("networkidle")
                # Navigate to backorder page
                page.goto(f"https://www.snapnames.com/domain/{domain}/backorder", timeout=15000)
                # Click "Place Backorder" button
                if page.locator("text=Place Backorder").is_visible():
                    page.click("text=Place Backorder")
                    page.wait_for_timeout(3000)
                    _post(f"Placed SnapNames backorder for {domain}", "info")
                    return True
                else:
                    _post(f"SnapNames: could not find backorder button for {domain}", "warning")
            except Exception as e:
                _post(f"SnapNames backorder error: {e}", "error")
            finally:
                browser.close()
    return False

def place_namejet_backorder(domain, username, password):
    """NameJet backorder via browser automation (no public API)."""
    if HAS_PLAYWRIGHT:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto("https://www.namejet.com/Login.aspx", timeout=20000)
                page.fill("input[name='ctl00$ContentPlaceHolder1$Login1$UserName']", username)
                page.fill("input[name='ctl00$ContentPlaceHolder1$Login1$Password']", password)
                page.click("input[name='ctl00$ContentPlaceHolder1$Login1$LoginButton']")
                page.wait_for_load_state("networkidle")
                # Go to backorder page for the domain
                page.goto(f"https://www.namejet.com/Pages/Backorders/BackorderDetails.aspx?domainname={domain}", timeout=15000)
                if page.locator("text=Place Backorder").is_visible():
                    page.click("text=Place Backorder")
                    page.wait_for_timeout(3000)
                    _post(f"Placed NameJet backorder for {domain}", "info")
                    return True
            except Exception as e:
                _post(f"NameJet backorder error: {e}", "error")
            finally:
                browser.close()
    return False

def place_dropcatch_backorder(domain):
    """DropCatch doesn't require login; backorder is placing a bid in the eventual auction. Done via page."""
    if HAS_PLAYWRIGHT:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(f"https://www.dropcatch.com/domain/{domain}", timeout=15000)
                # The button might be "Backorder Now" or "Place Backorder"
                if page.locator("text=Backorder").is_visible():
                    page.click("text=Backorder")
                    page.wait_for_timeout(3000)
                    _post(f"Placed DropCatch backorder for {domain}", "info")
                    return True
            except Exception as e:
                _post(f"DropCatch backorder error: {e}", "error")
            finally:
                browser.close()
    return False

def place_godaddy_backorder(domain, max_price=20):
    """GoDaddy Auctions backorder via browser."""
    if not HAS_PLAYWRIGHT:
        return False
    # This would require the user to be logged in (cookies). For research, we simulate.
    _post(f"Would place GoDaddy backorder for {domain} (max ${max_price}) — browser automation.", "info")
    return True  # stub

# ── Auction monitoring & bidding ───────────────────────────────────────────────
def monitor_snapnames_auctions(won_domains):
    """Check auction status of domains we have backordered and place bids."""
    # In reality, SnapNames sends email notifications; we can scrape the auction page.
    for domain, info in won_domains.items():
        if info.get("platform") != "snapnames":
            continue
        # Get current price
        try:
            url = f"https://www.snapnames.com/domain/{domain}/auction"
            resp = requests.get(url, timeout=10)
            if "auction ended" in resp.text.lower():
                continue
            price_match = re.search(r'Current\s*Bid[:\s]*\$?([\d,]+\.\d{2})', resp.text)
            if price_match:
                current_price = float(price_match.group(1).replace(",", ""))
                max_bid = CFG["auction"]["max_bid"]
                if current_price < max_bid and CFG["auction"]["auto_bid"]:
                    # Place bid via browser
                    if HAS_PLAYWRIGHT:
                        with sync_playwright() as p:
                            browser = p.chromium.launch(headless=True)
                            page = browser.new_page()
                            page.goto(url)
                            # fill bid amount and submit
                            page.fill("input[name='bidAmount']", str(min(current_price+10, max_bid)))
                            page.click("input[type='submit']")
                            page.wait_for_timeout(2000)
                            _post(f"Bid ${min(current_price+10, max_bid)} on {domain} (SnapNames)", "info")
                            browser.close()
        except Exception as e:
            _post(f"Auction check error for {domain}: {e}", "warning")

# ── Main scanning logic ────────────────────────────────────────────────────────
def domain_scan():
    state = load_state()
    domains = fetch_expiring_domains()
    if not domains:
        return
    filtered = filter_domains(domains)
    _post(f"Fetched {len(domains)} domains, {len(filtered)} passed filters.", "info")

    for dom in filtered:
        domain_name = dom["domain"]
        # Skip if already backordered
        if domain_name in state.get("backordered", {}):
            continue

        # Check backorder counts
        status = get_backorder_status(domain_name)
        _post(f"{domain_name} (DA{dom['moz_da']}) backorder counts: {status}", "info")

        # Decide where to backorder (pick services with fewest existing backorders)
        target_service = None
        min_count = 999
        for service, count in status.items():
            if service in ["snapnames", "namejet", "dropcatch", "godaddy"] and CFG["backorder_services"].get(service, {}).get("enabled"):
                if count < min_count:
                    min_count = count
                    target_service = service

        if target_service is None:
            continue

        # Execute backorder
        success = False
        if target_service == "snapnames" and CFG["backorder_services"]["snapnames"]["username"]:
            success = place_snapnames_backorder(domain_name, CFG["backorder_services"]["snapnames"]["username"], CFG["backorder_services"]["snapnames"]["password"])
        elif target_service == "namejet" and CFG["backorder_services"]["namejet"]["username"]:
            success = place_namejet_backorder(domain_name, CFG["backorder_services"]["namejet"]["username"], CFG["backorder_services"]["namejet"]["password"])
        elif target_service == "dropcatch":
            success = place_dropcatch_backorder(domain_name)
        elif target_service == "godaddy":
            success = place_godaddy_backorder(domain_name, CFG["backorder_services"]["godaddy"]["max_price"])

        if success:
            state.setdefault("backordered", {})[domain_name] = {
                "date": datetime.utcnow().isoformat(),
                "service": target_service,
                "moz_da": dom["moz_da"]
            }
            save_state(state)
            _post(f"Backordered {domain_name} on {target_service}", "info")
        time.sleep(2)  # be gentle

def auction_monitor():
    state = load_state()
    monitor_snapnames_auctions(state.get("won_domains", {}))
    # Extend for other platforms as needed
    save_state(state)

def main():
    _wait_for_hub()
    _post("Domain Drop‑Catching Bot online. Scanning expiring domains...", "info")

    # Start auction checker thread
    def auction_loop():
        while True:
            auction_monitor()
            time.sleep(AUCTION_CHECK_INTERVAL)
    threading.Thread(target=auction_loop, daemon=True).start()

    while True:
        domain_scan()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `domain_dropcatch_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "expired_domains": {
    "api_key": "your-key",
    "tld": "com",
    "min_domain_length": 3,
    "max_domain_length": 20,
    "max_dashes": 0,
    "min_moz_da": 15,
    "min_traffic": 100,
    "keywords": ["crypto", "nft", "ai"]
  },
  "backorder_services": {
    "snapnames": {
      "enabled": true,
      "username": "your-username",
      "password": "your-password"
    },
    "namejet": {
      "enabled": true,
      "username": "your-username",
      "password": "your-password"
    },
    "dropcatch": {
      "enabled": true,
      "max_backorder_attempts": 1
    },
    "godaddy": {
      "enabled": false,
      "max_price": 20.0
    }
  },
  "auction": {
    "auto_bid": false,
    "max_bid": 100.0
  }
}
"""
