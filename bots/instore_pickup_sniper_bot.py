#!/usr/bin/env python3
"""
instore_pickup_sniper_bot.py — In‑Store Pickup Sniper Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors store inventory APIs for high‑demand items.
2. Instantly purchases for curbside pickup when stock appears.
3. Alerts you to resell the pickup confirmation locally.

Targets: Best Buy (RTX cards, consoles) and Target (Pokémon ETBs).
Uses real inventory APIs + Playwright for checkout.

SETUP
─────
1. Install dependencies:
      pip install playwright requests
      python -m playwright install chromium

2. Set 2Captcha API key (if needed): export CAPTCHA_API_KEY="your-key"

3. Create `pickup_sniper_config.json` (example at bottom).
   Fill in your accounts, payment details, zip code, and target products.

4. Attach to BotController.
"""

import json, os, re, time, random, threading, requests
from datetime import datetime, timedelta

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── BotController connection ─────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "instore_pickup_sniper_bot"
BOT_NAME = "In‑Store Pickup Sniper"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pickup_sniper_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "pickup_sniper_state.json")

SCAN_INTERVAL      = 15  # seconds between API calls
HEARTBEAT_INTERVAL = 30
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

# ── Config & State ──────────────────────────────────────────────────────────
def load_config():
    default = {
        "zip_code": "10001",
        "bestbuy_account": {
            "email": "your@email.com",
            "password": "your_password",
            "profile": {
                "first_name": "John", "last_name": "Doe",
                "address": "123 Main St", "city": "New York", "state": "NY", "zip": "10001",
                "phone": "5551234567",
                "card_number": "4111111111111111",
                "card_expiry": "12/28", "card_cvv": "123"
            }
        },
        "target_account": {
            "email": "target@email.com",
            "password": "target_pass",
            "profile": {
                "first_name": "Jane", "last_name": "Doe",
                "address": "456 Oak Ave", "city": "New York", "state": "NY", "zip": "10001",
                "phone": "5559876543",
                "card_number": "5555555555554444",
                "card_expiry": "11/27", "card_cvv": "321"
            }
        },
        "products": [
            {
                "retailer": "bestbuy",
                "name": "RTX 5090 FE",
                "sku": "6521434",
                "url": "https://www.bestbuy.com/site/nvidia-geforce-rtx-5090-founders-edition/6521434.p?skuId=6521434",
                "max_price": 1999.99
            },
            {
                "retailer": "target",
                "name": "Pokémon SV 151 Elite Trainer Box",
                "tcin": "89612679",
                "url": "https://www.target.com/p/pokemon-scarlet-violet-151-elite-trainer-box/-/A-89612679",
                "max_price": 54.99
            }
        ],
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "resell": {
            "alert_method": "dashboard",   # "dashboard" only; manual resell via alert
            "markup_multiplier": 2.0
        }
    }
    if not os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "w") as f: json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f: return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE): return {"purchased": []}
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

# ── Inventory APIs ──────────────────────────────────────────────────────────
def check_bestbuy_stock(sku, zip_code):
    """Return list of stores with stock: [{store_id, name, quantity}]"""
    url = f"https://www.bestbuy.com/api/tcfb/model.html?method=getStoreAvailability&skuId={sku}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        stores = []
        for store in data.get("ispuStoreAvailability", {}).get("stores", []):
            store_id = store["storeId"]
            name = store["storeName"]
            # Check if the store can fulfill pickup online
            eligible = store.get("onlineStorePickupAvailability", {}).get("availabilityStatus", "")=="Available"
            if eligible:
                stores.append({"store_id": store_id, "name": name, "quantity": store.get("quantity", 0)})
        return stores
    except Exception as e:
        _post(f"Best Buy API error: {e}", "warning")
        return []

def check_target_stock(tcin, zip_code):
    """Return list of stores with stock for Drive Up / Pickup."""
    # Target's API requires an API key (found in the Target app). We'll use a known public key.
    api_key = "9f36aeafbe60771e321a7cc95a78140772ab3e95"  # public key from Target app
    url = f"https://api.target.com/fulfillment_aggregator/v1/fiats/{tcin}"
    params = {
        "key": api_key,
        "nearby": zip_code,
        "limit": 10,
        "requested_quantity": 1,
        "radius": 50,
        "fulfillment_test_mode": "grocery_opu_team_member_test"
    }
    headers = {"User-Agent": "Target/10.39.0 (iPhone; iOS 16.0; Scale/3.0)"}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        data = resp.json()
        stores = []
        for product in data.get("data", {}).get("list", {}).get("products", []):
            store_id = product.get("store_id")
            store_name = product.get("store_name", "Target")
            availability = product.get("fulfillment", {}).get("store_options", [])
            for opt in availability:
                if opt.get("fulfillment_type") in ("PICKUP", "DRIVEUP"):
                    if opt.get("available_to_promise_quantity", 0) > 0:
                        stores.append({"store_id": store_id, "name": store_name, "quantity": opt["available_to_promise_quantity"]})
                        break
        return stores
    except Exception as e:
        _post(f"Target API error: {e}", "warning")
        return []

# ── Purchase flows ──────────────────────────────────────────────────────────
def purchase_bestbuy(product, store_id, account):
    """Checkout via Best Buy with in‑store pickup."""
    if not HAS_PLAYWRIGHT: return False
    profile = account["profile"]
    proxy = next_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        try:
            # Log in
            _post("Logging into Best Buy...", "info")
            page.goto("https://www.bestbuy.com/identity/signin", wait_until="networkidle", timeout=30000)
            page.fill("input#fld-e", account["email"])
            page.fill("input#fld-p1", account["password"])
            page.click("button.cia-form__controls__submit")
            page.wait_for_load_state("networkidle")

            # ATC with pickup store selected
            atc_url = f"https://api.bestbuy.com/click/-/{product['sku']}/cart?fulfillment=storePickup&storeId={store_id}"
            _post(f"Adding to cart for store {store_id}...", "info")
            page.goto(atc_url, wait_until="networkidle", timeout=15000)

            # Proceed to checkout
            page.goto("https://www.bestbuy.com/cart", wait_until="networkidle")
            page.click("button:has-text('Checkout')")
            page.wait_for_load_state("networkidle")

            # Fill payment if needed (usually saved)
            # Click place order
            page.click("button:has-text('Place Your Order')")
            page.wait_for_timeout(10000)
            if "thank you" in page.content().lower():
                _post(f"Best Buy order placed for {product['name']} at store {store_id}!", "info")
                return True
            return False
        except Exception as e:
            _post(f"Best Buy purchase error: {e}", "error")
            return False
        finally:
            browser.close()

def purchase_target(product, store_id, account):
    """Checkout on Target.com for Drive Up / Pickup."""
    if not HAS_PLAYWRIGHT: return False
    profile = account["profile"]
    proxy = next_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        try:
            # Log in
            _post("Logging into Target...", "info")
            page.goto("https://www.target.com/login", wait_until="networkidle", timeout=30000)
            page.fill("input#username", account["email"])
            page.fill("input#password", account["password"])
            page.click("button#login")
            page.wait_for_load_state("networkidle")

            # ATC with pickup enabled (via Target's "Add to cart" direct link)
            # We can use: https://www.target.com/p/-/A-<TCIN>?fulfillment=pickup&storeId=<store_id>
            atc_url = f"https://www.target.com/p/-/A-{product['tcin']}?fulfillment=pickup&storeId={store_id}"
            page.goto(atc_url, wait_until="networkidle")
            # Click "Pick it up" button if present
            page.click("button:has-text('Pick it up')")
            page.wait_for_timeout(1000)

            # Add to cart
            page.click("button[data-test='addToCartButton']")
            page.wait_for_timeout(1000)

            # Go to cart
            page.goto("https://www.target.com/cart", wait_until="networkidle")
            page.click("button:has-text('Checkout')")
            page.wait_for_load_state("networkidle")

            # Fill payment if needed (saved payment list)
            # Place order
            page.click("button:has-text('Place your order')")
            page.wait_for_timeout(10000)
            if "thank you" in page.content().lower():
                _post(f"Target order placed for {product['name']} at store {store_id}!", "info")
                return True
            return False
        except Exception as e:
            _post(f"Target purchase error: {e}", "error")
            return False
        finally:
            browser.close()

# ── Resell alert ────────────────────────────────────────────────────────────
def alert_resell(product_name, retailer, store_name, price, markup):
    resell_price = round(price * markup, 2)
    msg = f"🔥 RESELL OPPORTUNITY: {product_name} ({retailer}) ready for pickup at {store_name}. Paid ${price}, resell for ${resell_price}. Act fast!"
    _post(msg, "error", {"product": product_name, "store": store_name, "price_paid": price, "resell_price": resell_price})
    # Could also attempt to post to local marketplace via API if desired (not implemented)

# ── Main sniper loop ────────────────────────────────────────────────────────
def sniper():
    state = load_state()
    zip_code = CFG.get("zip_code", "10001")
    for product in CFG["products"]:
        retailer = product["retailer"]
        if retailer == "bestbuy":
            account = CFG.get("bestbuy_account", {})
            stores = check_bestbuy_stock(product["sku"], zip_code)
            if not stores:
                _post(f"Best Buy: {product['name']} – no in‑store stock nearby.", "info")
                continue
            for store in stores:
                # Check if already purchased for this store/sku
                key = f"{retailer}_{product['sku']}_{store['store_id']}"
                if any(p.get("key") == key for p in state.get("purchased", [])):
                    continue
                _post(f"🚀 Best Buy {product['name']} IN STOCK at {store['name']}! Starting checkout...", "error")
                success = purchase_bestbuy(product, store["store_id"], account)
                if success:
                    state.setdefault("purchased", []).append({
                        "key": key,
                        "retailer": retailer,
                        "name": product["name"],
                        "store": store["name"],
                        "timestamp": datetime.utcnow().isoformat(),
                        "price": product.get("max_price", 0)
                    })
                    save_state(state)
                    alert_resell(product["name"], retailer, store["name"], product.get("max_price", 0),
                                 CFG["resell"].get("markup_multiplier", 2.0))
                    # Break after one successful purchase to avoid multiple (configurable)
                    break
                else:
                    _post("Checkout failed. Continuing...", "warning")
        elif retailer == "target":
            account = CFG.get("target_account", {})
            stores = check_target_stock(product["tcin"], zip_code)
            if not stores:
                _post(f"Target: {product['name']} – no in‑store stock nearby.", "info")
                continue
            for store in stores:
                key = f"{retailer}_{product['tcin']}_{store['store_id']}"
                if any(p.get("key") == key for p in state.get("purchased", [])):
                    continue
                _post(f"🚀 Target {product['name']} IN STOCK at {store['name']}! Starting checkout...", "error")
                success = purchase_target(product, store["store_id"], account)
                if success:
                    state.setdefault("purchased", []).append({
                        "key": key,
                        "retailer": retailer,
                        "name": product["name"],
                        "store": store["name"],
                        "timestamp": datetime.utcnow().isoformat(),
                        "price": product.get("max_price", 0)
                    })
                    save_state(state)
                    alert_resell(product["name"], retailer, store["name"], product.get("max_price", 0),
                                 CFG["resell"].get("markup_multiplier", 2.0))
                    break
                else:
                    _post("Checkout failed. Continuing...", "warning")

def main():
    wait_for_hub()
    _post("In‑Store Pickup Sniper Bot online. Monitoring store inventory APIs.", "info")
    while True:
        sniper()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
