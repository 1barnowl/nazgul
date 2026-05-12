#!/usr/bin/env python3
"""
server_parts_flipper_bot.py — Server Parts Parting‑Out Arbitrage Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Scrapes liquidation marketplaces for decommissioned server lots.
2. Reads manifests to determine included components.
3. Estimates total part‑out resale value using eBay sold prices.
4. If profit > threshold, optionally places a bid via Playwright.
5. After purchase, auto‑lists individual components on eBay.

✦ PURELY FOR RESEARCH / EDUCATIONAL PURPOSES.
  Automated bidding may violate auction site Terms of Service.
  Use only on authorized test accounts.

SETUP
─────
1. Install dependencies:
      pip install playwright beautifulsoup4 requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key (if needed):
      export CAPTCHA_API_KEY="your-key"

3. Set eBay API credentials for finding sold prices and listing:
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `server_parts_config.json` (example at bottom).
   Fill in:
   - Liquidation marketplace details (URL, selectors).
   - Your eBay account for price research.
   - Bidder account credentials (optional).
   - Payment details (for manual reference).

5. Attach to BotController.
"""

import json, os, re, time, random, threading, requests, csv, io
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from urllib.parse import urljoin

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

try:
    from ebaysdk.finding import Connection as eBayFinding
    from ebaysdk.trading import Connection as eBayTrading
    HAS_EBAY_SDK = True
except ImportError:
    HAS_EBAY_SDK = False

# ── BotController connection ─────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "server_parts_flipper_bot"
BOT_NAME = "Server Parts Flipper"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "server_parts_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "server_parts_state.json")

SCAN_INTERVAL      = 3600   # 1 hour between scans
HEARTBEAT_INTERVAL = 30

_last_hb = 0.0
_lock = threading.Lock()

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
    with _lock:
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

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).ok: return
        except Exception:
            pass
        time.sleep(1)

# ── Config / State ──────────────────────────────────────────────────────────
def load_config():
    default = {
        "marketplace": {
            "name": "B-Stock (Tech Liquidators)",
            "listings_url": "https://bstock.com/tech-liquidation/",
            "listing_selector": "div.lot-card",
            "title_selector": "h3",
            "bid_selector": "span.current-bid",
            "time_left_selector": "span.time-left",
            "manifest_link_selector": "a.manifest-link",
            "lot_detail_link_selector": "a.lot-link"
        },
        "bidding": {
            "auto_bid": False,
            "max_bid_percent_of_retail": 40,   # bid up to X% of total part‑out value
            "account": {
                "email": "your_bidder@email.com",
                "password": "your_password",
                "profile": {
                    "first_name": "Techie", "last_name": "Server",
                    "address": "1 Data Center Dr", "city": "Ashburn", "state": "VA", "zip": "20147",
                    "phone": "7035551234",
                    "card_number": "4111111111111111", "card_expiry": "12/28", "card_cvv": "123"
                }
            }
        },
        "ebay": {
            "finding_api": True,      # set True if you have EBAY_APP_ID for completed item pricing
            "auto_list": True,
            "markup_multiplier": 1.2,
            "listing_duration_days": 30
        },
        "part_types": {
            "cpu": {"keywords": ["Xeon", "EPYC"], "category_id": "164", "price_weight": 1.0},
            "ram": {"keywords": ["DDR4", "DDR5", "ECC", "Server Memory"], "category_id": "170083", "price_weight": 0.9},
            "hdd": {"keywords": ["SAS", "Enterprise HDD", "Seagate Exos"], "category_id": "170083", "price_weight": 0.8},
            "ssd": {"keywords": ["NVMe", "Enterprise SSD", "Intel P"], "category_id": "170083", "price_weight": 1.0},
            "gpu": {"keywords": ["NVIDIA Tesla", "AMD Instinct"], "category_id": "273", "price_weight": 1.2}
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"}
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"bid_on": [], "won": []}
    with open(STATE_FILE, "r") as f: return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

CFG = load_config()
STATE = load_state()

# ── Proxy rotation ──────────────────────────────────────────────────────────
_proxies = CFG.get("proxies", {}).get("list", [])
_proxy_idx = 0
def next_proxy():
    global _proxy_idx
    if not _proxies: return None
    with _lock:
        p = _proxies[_proxy_idx % len(_proxies)]
        _proxy_idx += 1; return p

# ── Captcha solver ──────────────────────────────────────────────────────────
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

# ── eBay price research ─────────────────────────────────────────────────────
def get_ebay_sold_price_avg(keyword, condition="used"):
    """Return average sold price in USD, or None."""
    if not HAS_EBAY_SDK:
        return _scrape_ebay_sold(keyword)
    try:
        api = eBayFinding(appid=os.getenv("EBAY_APP_ID"), config_file=None)
        response = api.execute("findCompletedItems", {
            "keywords": keyword,
            "itemFilter": [
                {"name": "Condition", "value": condition.capitalize()},
                {"name": "SoldItemsOnly", "value": "true"}
            ],
            "paginationInput": {"entriesPerPage": 5}
        })
        items = response.dict().get("searchResult", {}).get("item", [])
        prices = []
        for itm in items:
            selling = itm.get("sellingStatus", {})
            price = selling.get("convertedCurrentPrice", {}).get("value")
            if price: prices.append(float(price))
        if prices: return sum(prices) / len(prices)
    except Exception as e:
        _post(f"eBay Finding API error: {e}", "warning")
    return _scrape_ebay_sold(keyword)

def _scrape_ebay_sold(keyword):
    """Fallback: scrape eBay sold search."""
    try:
        url = f"https://www.ebay.com/sch/i.html?_nkw={requests.utils.quote(keyword)}&LH_Sold=1&LH_Complete=1"
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        if resp.status_code != 200: return None
        soup = BeautifulSoup(resp.text, "html.parser")
        prices = []
        for price_el in soup.select(".s-item__price"):
            nums = re.findall(r'[\d,]+\.?\d{0,2}', price_el.get_text().replace(",", ""))
            if nums: prices.append(float(nums[0]))
        if prices: return sum(prices) / len(prices)
    except: pass
    return None

# ── Manifest parser ─────────────────────────────────────────────────────────
def fetch_and_parse_manifest(manifest_url):
    """Download manifest (CSV or HTML table) and return list of dicts: {type, description, quantity}."""
    items = []
    try:
        resp = requests.get(manifest_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        content_type = resp.headers.get("Content-Type", "")
        if "csv" in content_type or manifest_url.endswith(".csv"):
            reader = csv.reader(io.StringIO(resp.text))
            header = next(reader, [])
            # Try to find columns: 'Part Name', 'Qty'
            name_idx = next((i for i, h in enumerate(header) if "part" in h.lower() or "description" in h.lower()), 0)
            qty_idx = next((i for i, h in enumerate(header) if "qty" in h.lower() or "quantity" in h.lower()), 1)
            for row in reader:
                if len(row) < max(name_idx, qty_idx)+1: continue
                desc = row[name_idx].strip()
                try: qty = int(row[qty_idx])
                except: qty = 1
                items.append({"description": desc, "quantity": qty})
        else:
            # HTML table parsing
            soup = BeautifulSoup(resp.text, "html.parser")
            table = soup.find("table")
            if not table: return items
            rows = table.find_all("tr")[1:]  # skip header
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 2:
                    desc = cols[0].get_text(strip=True)
                    try: qty = int(cols[1].get_text(strip=True))
                    except: qty = 1
                    items.append({"description": desc, "quantity": qty})
    except Exception as e:
        _post(f"Manifest parse error: {e}", "warning")
    return items

def classify_component(description):
    """Return part type string or None."""
    desc_lower = description.lower()
    for ptype, defn in CFG["part_types"].items():
        for kw in defn["keywords"]:
            if kw.lower() in desc_lower:
                return ptype
    return None

# ── Valuation of a lot ─────────────────────────────────────────────────────
def estimate_partout_value(manifest_items):
    """Return total estimated value, details list."""
    total = 0.0
    details = []
    for item in manifest_items:
        desc = item["description"]
        qty = item["quantity"]
        part_type = classify_component(desc)
        if not part_type:
            continue  # skip unknown parts
        avg_price = get_ebay_sold_price_avg(desc)
        if avg_price is None:
            # Try a broader keyword
            avg_price = get_ebay_sold_price_avg(f"{part_type} server {desc.split()[0]}")
        if avg_price is None:
            continue
        price_weight = CFG["part_types"].get(part_type, {}).get("price_weight", 1.0)
        effective_price = avg_price * price_weight
        total += effective_price * qty
        details.append({
            "description": desc,
            "type": part_type,
            "quantity": qty,
            "avg_sold_price": round(avg_price, 2),
            "effective_price": round(effective_price, 2),
            "subtotal": round(effective_price * qty, 2)
        })
    return total, details

# ── Scrape listings ────────────────────────────────────────────────────────
def scrape_listings():
    cfg = CFG["marketplace"]
    try:
        resp = requests.get(cfg["listings_url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        _post(f"Failed to fetch listings: {e}", "error")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    cards = soup.select(cfg["listing_selector"])
    lots = []
    for card in cards:
        title_el = card.select_one(cfg["title_selector"])
        bid_el = card.select_one(cfg["bid_selector"])
        manifest_el = card.select_one(cfg.get("manifest_link_selector"))
        detail_el = card.select_one(cfg.get("lot_detail_link_selector"))
        if not title_el or not bid_el: continue
        title = title_el.get_text(strip=True)
        bid_text = bid_el.get_text(strip=True)
        current_bid = float(re.sub(r'[^\d.]', '', bid_text)) if re.sub(r'[^\d.]', '', bid_text) else 0.0
        manifest_url = manifest_el.get("href") if manifest_el else None
        if manifest_url and not manifest_url.startswith("http"):
            manifest_url = urljoin(cfg["listings_url"], manifest_url)
        lot_url = detail_el.get("href") if detail_el else None
        if lot_url and not lot_url.startswith("http"):
            lot_url = urljoin(cfg["listings_url"], lot_url)
        lots.append({
            "title": title,
            "current_bid": current_bid,
            "manifest_url": manifest_url,
            "lot_url": lot_url,
            "id": lot_url or title
        })
    return lots

# ── Bidding automation (Playwright) ────────────────────────────────────────
def place_bid(lot_url, max_bid, account):
    if not HAS_PLAYWRIGHT or not CFG["bidding"].get("auto_bid"): return False
    proxy = next_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        try:
            _post(f"Navigating to {lot_url} to place bid...", "info")
            page.goto(lot_url, wait_until="networkidle", timeout=30000)
            # Login if needed
            if page.locator("input[type='email'], input[name='email']").is_visible():
                page.fill("input[type='email'], input[name='email']", account["email"])
                page.fill("input[type='password'], input[name='password']", account["password"])
                page.click("button[type='submit']")
                page.wait_for_load_state("networkidle")
            # Enter bid
            page.fill("input[name='bidAmount'], input#bid-amount", str(max_bid))
            # Submit bid
            page.click("button:has-text('Place Bid'), input[type='submit']")
            page.wait_for_timeout(3000)
            if "bid confirmed" in page.content().lower():
                _post(f"Bid placed: ${max_bid}", "info")
                return True
            _post("Bid may have failed.", "warning")
            return False
        except Exception as e:
            _post(f"Bid error: {e}", "error")
            return False
        finally:
            browser.close()

# ── eBay listing for parted components ─────────────────────────────────────
def list_component_on_ebay(title, price, category_id, qty=1):
    if not HAS_EBAY_SDK or not CFG["ebay"].get("auto_list"): return False
    trading = eBayTrading(
        domain="api.ebay.com",
        appid=os.getenv("EBAY_APP_ID"),
        certid=os.getenv("EBAY_CERT_ID"),
        devid=os.getenv("EBAY_DEV_ID"),
        token=os.getenv("EBAY_AUTH_TOKEN"),
        config_file=None
    )
    if not trading: return False
    markup = CFG["ebay"].get("markup_multiplier", 1.2)
    ebay_price = round(price * markup, 2)
    payload = {
        "Item": {
            "Title": f"{title} – Working Pull",
            "Description": f"Fully tested {title}. Pulled from decommissioned server. Ships fast.",
            "PrimaryCategory": {"CategoryID": category_id},
            "StartPrice": ebay_price,
            "Quantity": qty,
            "ListingDuration": f"Days_{CFG['ebay']['listing_duration_days']}",
            "Country": "US", "Currency": "USD",
            "ListingType": "FixedPriceItem", "Site": "US",
            "ConditionID": "3000"  # Used
        }
    }
    try:
        response = trading.execute("AddFixedPriceItem", payload)
        if response.dict().get("Ack") == "Success":
            _post(f"Listed {title} x{qty} on eBay at ${ebay_price}", "info")
            return True
    except Exception as e:
        _post(f"eBay listing error: {e}", "error")
    return False

# ── Main scanner ───────────────────────────────────────────────────────────
def scan_liquidation_lots():
    lots = scrape_listings()
    if not lots:
        _post("No lots found.", "info")
        return
    _post(f"Found {len(lots)} lots.", "info")
    for lot in lots:
        # Skip if already bid/processed
        if lot["id"] in [b.get("lot_id") for b in STATE.get("bid_on", [])]:
            continue
        # If manifest URL available, parse it
        manifest_items = []
        if lot.get("manifest_url"):
            manifest_items = fetch_and_parse_manifest(lot["manifest_url"])
        if not manifest_items:
            _post(f"No manifest for {lot['title'][:50]}... skipping.", "info")
            continue
        partout_value, details = estimate_partout_value(manifest_items)
        if partout_value <= 0:
            continue
        profit = partout_value - lot["current_bid"]
        profit_pct = (profit / lot["current_bid"] * 100) if lot["current_bid"] > 0 else 0
        max_bid_pct = CFG["bidding"]["max_bid_percent_of_retail"]
        eligible = profit_pct >= 20 and (lot["current_bid"] <= partout_value * max_bid_pct / 100)
        _post(f"Lot: {lot['title'][:60]} | Bid ${lot['current_bid']:.0f} | "
              f"Part‑out Value: ${partout_value:.0f} | Profit: ${profit:.0f} ({profit_pct:.0f}%)",
              "warning" if eligible else "info")
        if eligible:
            _post(f"✅ ARBITRAGE: {lot['title'][:60]} – Consider bidding up to ${partout_value * max_bid_pct/100:.0f}",
                  "error", {"lot_id": lot["id"], "partout_value": partout_value, "current_bid": lot["current_bid"]})
            # Auto-bid if enabled
            if CFG.get("bidding", {}).get("auto_bid"):
                max_bid = round(partout_value * max_bid_pct / 100, 2)
                account = CFG["bidding"].get("account", {})
                if account and lot.get("lot_url"):
                    success = place_bid(lot["lot_url"], max_bid, account)
                    if success:
                        STATE.setdefault("bid_on", []).append({
                            "lot_id": lot["id"],
                            "max_bid": max_bid,
                            "timestamp": datetime.utcnow().isoformat()
                        })
                        save_state(STATE)
                        # Part-out and listing won't happen until we know we won.
                        # A separate "fulfillment" check could use email monitoring.
        # If we previously won this lot (simulated), perform part-out listing
        if lot["id"] in [w.get("lot_id") for w in STATE.get("won", [])]:
            for part in details:
                category = CFG["part_types"].get(part["type"], {}).get("category_id", "170083")
                list_component_on_ebay(part["description"], part["effective_price"], category, part["quantity"])

def main():
    wait_for_hub()
    _post("Server Parts Parting‑Out Bot online.", "info")
    while True:
        scan_liquidation_lots()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `server_parts_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "marketplace": {
    "name": "B-Stock Tech Liquidation",
    "listings_url": "https://bstock.com/tech-liquidation/",
    "listing_selector": "div.lot-card",
    "title_selector": "h3",
    "bid_selector": "span.current-bid",
    "manifest_link_selector": "a.manifest-link",
    "lot_detail_link_selector": "a.lot-link"
  },
  "bidding": {
    "auto_bid": false,
    "max_bid_percent_of_retail": 40,
    "account": {
      "email": "your_bidder@email.com",
      "password": "your_password",
      "profile": {
        "first_name": "Techie",
        "last_name": "Server",
        "address": "1 Data Center Dr",
        "city": "Ashburn",
        "state": "VA",
        "zip": "20147",
        "phone": "7035551234",
        "card_number": "4111111111111111",
        "card_expiry": "12/28",
        "card_cvv": "123"
      }
    }
  },
  "ebay": {
    "finding_api": true,
    "auto_list": true,
    "markup_multiplier": 1.2,
    "listing_duration_days": 30
  },
  "part_types": {
    "cpu": {"keywords": ["Xeon", "EPYC"], "category_id": "164", "price_weight": 1.0},
    "ram": {"keywords": ["DDR4", "DDR5", "ECC", "Server Memory"], "category_id": "170083", "price_weight": 0.9}
  },
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"}
}
"""
