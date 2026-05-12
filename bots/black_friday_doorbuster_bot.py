#!/usr/bin/env python3
"""
black_friday_doorbuster_bot.py — Doorbuster Arbitrage Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Monitors product pages for flash‑sale / doorbuster prices.
2. Instantly maxes out purchase limits when price drops below target.
3. Auto‑checks out via Playwright with proxy rotation and 2Captcha.
4. Optionally lists the items on eBay after the sale ends.

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated purchasing may violate retailer Terms of Service.

SETUP
─────
1. Install dependencies:
      pip install playwright requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `doorbuster_config.json` (example at bottom).
   Fill in:
   - Your retailer accounts and payment details.
   - List of target doorbusters with product URLs, target price thresholds, max quantity.

5. Attach to BotController.
"""

import json, os, re, time, random, threading, requests
from datetime import datetime
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

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "black_friday_doorbuster_bot"
BOT_NAME = "Black Friday Doorbuster Bot"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "doorbuster_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "doorbuster_state.json")

SCAN_INTERVAL      = 30    # seconds between price checks
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
        "accounts": {
            "bestbuy": {
                "email": "your@email.com",
                "password": "your_password",
                "profile": {
                    "first_name": "John", "last_name": "Doe",
                    "address": "123 Main St", "city": "New York", "state": "NY", "zip": "10001",
                    "phone": "5551234567",
                    "card_number": "4111111111111111", "card_expiry": "12/28", "card_cvv": "123"
                }
            },
            "amazon": {
                "email": "amazon@email.com",
                "password": "amazon_pass",
                "profile": {
                    "first_name": "Jane", "last_name": "Doe",
                    "address": "456 Oak Ave", "city": "New York", "state": "NY", "zip": "10001",
                    "phone": "5559876543",
                    "card_number": "5555555555554444", "card_expiry": "11/27", "card_cvv": "321"
                }
            },
            "walmart": {
                "email": "walmart@email.com",
                "password": "walmart_pass",
                "profile": {
                    "first_name": "Mike", "last_name": "Smith",
                    "address": "789 Pine Blvd", "city": "Bentonville", "state": "AR", "zip": "72712",
                    "phone": "4795551234",
                    "card_number": "378282246310005", "card_expiry": "10/26", "card_cvv": "123"
                }
            }
        },
        "doorbusters": [
            {
                "retailer": "bestbuy",
                "sku": "6521434",
                "name": "RTX 5090 FE",
                "url": "https://www.bestbuy.com/site/nvidia-geforce-rtx-5090-founders-edition/6521434.p?skuId=6521434",
                "target_price": 1499.99,
                "max_qty": 2
            },
            {
                "retailer": "amazon",
                "asin": "B0DXYZ",
                "name": "85\" Samsung QLED 8K",
                "url": "https://www.amazon.com/dp/B0DXYZ",
                "target_price": 2999.99,
                "max_qty": 1
            },
            {
                "retailer": "walmart",
                "sku": "555123456",
                "name": "HP Victus Gaming PC (i7-13700HX, RTX 4070)",
                "url": "https://www.walmart.com/ip/555123456",
                "target_price": 899.00,
                "max_qty": 2
            }
        ],
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {
            "enabled": False,
            "markup_multiplier": 1.5,
            "list_after_purchase": True
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

# ── eBay listing ──────────────────────────────────────────────────────────────
def list_on_ebay(item_name, purchase_price):
    if not HAS_EBAY or not CFG["ebay"].get("enabled"): return
    trading = eBayTrading(
        domain="api.ebay.com",
        appid=os.getenv("EBAY_APP_ID"), certid=os.getenv("EBAY_CERT_ID"),
        devid=os.getenv("EBAY_DEV_ID"), token=os.getenv("EBAY_AUTH_TOKEN"),
        config_file=None
    )
    if not trading: return
    markup = CFG["ebay"]["markup_multiplier"]
    ebay_price = round(purchase_price * markup, 2)
    payload = {
        "Item": {
            "Title": f"{item_name} – Brand New In Box",
            "Description": f"Brand new, sealed. Ships immediately. Doorbuster deal.",
            "PrimaryCategory": {"CategoryID": "171833"},
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
            _post(f"eBay listing created: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e:
        _post(f"eBay error: {e}", "error")

# ── Retailer Modules ────────────────────────────────────────────────────────

def get_current_price_bestbuy(sku):
    """Try to fetch price from Best Buy API or scrape product page."""
    try:
        url = f"https://www.bestbuy.com/api/tcfb/product?sku={sku}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            price = data.get("pricing", {}).get("currentPrice", 0)
            if price: return float(price)
    except: pass
    # Fallback to scraping
    try:
        page_url = f"https://www.bestbuy.com/site/-/{sku}.p?skuId={sku}"
        html = requests.get(page_url, timeout=10).text
        soup = BeautifulSoup(html, "html.parser")
        price_elem = soup.select_one(".priceView-heroPrice span")
        if price_elem:
            price_text = price_elem.get_text(strip=True)
            price = re.sub(r'[^\d.]', '', price_text)
            if price: return float(price)
    except: pass
    return None

def get_current_price_amazon(asin):
    """Scrape Amazon product page or use API."""
    try:
        url = f"https://www.amazon.com/dp/{asin}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            # Look for price whole/decimal
            whole = soup.select_one("span.a-price-whole")
            fraction = soup.select_one("span.a-price-fraction")
            if whole and fraction:
                price_str = whole.get_text(strip=True).replace(",", "") + "." + fraction.get_text(strip=True)
                return float(price_str)
    except: pass
    return None

def get_current_price_walmart(sku):
    """Scrape Walmart product page."""
    try:
        url = f"https://www.walmart.com/ip/{sku}"
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            price_elem = soup.select_one("[itemprop='price']")
            if price_elem:
                price = price_elem.get("content")
                if price: return float(price)
    except: pass
    return None

def should_buy(current_price, target_price, max_qty, already_purchased_qty):
    """Check if price is at or below target and we haven't exceeded purchase limit."""
    if current_price is None or current_price > target_price:
        return False
    if already_purchased_qty >= max_qty:
        return False
    return True

# ── Purchase Modules (Playwright) ───────────────────────────────────────────
def purchase_bestbuy(product, account, qty):
    if not HAS_PLAYWRIGHT: return False
    profile = account["profile"]
    proxy = next_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        try:
            # Login
            _post("Logging into Best Buy...", "info")
            page.goto("https://www.bestbuy.com/identity/signin", wait_until="networkidle", timeout=30000)
            page.fill("input#fld-e", account["email"])
            page.fill("input#fld-p1", account["password"])
            page.click("button.cia-form__controls__submit")
            page.wait_for_load_state("networkidle")

            # Add to cart with quantity
            sku = product["sku"]
            atc_url = f"https://api.bestbuy.com/click/-/{sku}/cart?quantity={qty}"
            page.goto(atc_url, wait_until="networkidle", timeout=15000)

            # Checkout
            page.goto("https://www.bestbuy.com/cart", wait_until="networkidle")
            page.click("button:has-text('Checkout')")
            page.wait_for_load_state("networkidle")

            # Fill payment if needed
            _fill_bestbuy_checkout(page, profile)
            if page.locator("[data-sitekey]").count():
                solve_captcha(page)
            page.click("button:has-text('Place Your Order')")
            page.wait_for_timeout(10000)
            if "thank you" in page.content().lower():
                return True
            return False
        except Exception as e:
            _post(f"Best Buy purchase error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_bestbuy_checkout(page, profile):
    try:
        page.fill("input#consolidatedAddresses_shippingAddress_1_firstName", profile.get("first_name",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_lastName", profile.get("last_name",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_address", profile.get("address",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_city", profile.get("city",""))
        page.select_option("select#consolidatedAddresses_shippingAddress_1_state", profile.get("state",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_zipcode", profile.get("zip",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_phone", profile.get("phone",""))
    except: pass
    try:
        page.fill("input#optimized-cc-card-number", profile["card_number"])
        page.fill("input[name='expirationDate']", profile["card_expiry"])
        page.fill("input#credit-card-cvv", profile["card_cvv"])
    except: pass

def purchase_amazon(product, account, qty):
    if not HAS_PLAYWRIGHT: return False
    profile = account["profile"]
    proxy = next_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        try:
            # Login
            _post("Logging into Amazon...", "info")
            page.goto("https://www.amazon.com/ap/signin", wait_until="networkidle", timeout=30000)
            page.fill("input#ap_email", account["email"])
            page.click("input#continue")
            page.wait_for_selector("input#ap_password", timeout=5000)
            page.fill("input#ap_password", account["password"])
            page.click("input#signInSubmit")
            page.wait_for_load_state("networkidle")

            # ATC with desired quantity
            asin = product["asin"]
            atc_url = f"https://www.amazon.com/gp/product/handle-buy-box/ref=dp_start-bbf_1_glance?ie=UTF8&asin={asin}&quantity={qty}"
            page.goto(atc_url, wait_until="networkidle", timeout=15000)

            # Checkout
            page.goto("https://www.amazon.com/gp/cart/view.html", wait_until="networkidle")
            page.click("input[name='proceedToRetailCheckout']")
            page.wait_for_load_state("networkidle")
            # Fill address/payment if needed
            _fill_amazon_checkout(page, profile)
            if page.locator("[data-sitekey]").count():
                solve_captcha(page)
            page.click("input[name='placeYourOrder1']")
            page.wait_for_timeout(10000)
            if "thank you" in page.content().lower():
                return True
            return False
        except Exception as e:
            _post(f"Amazon purchase error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_amazon_checkout(page, profile):
    try:
        page.fill("input#enterAddressAddressLine1", profile.get("address",""))
        page.fill("input#enterAddressCity", profile.get("city",""))
        page.select_option("select#enterAddressStateOrRegion", profile.get("state",""))
        page.fill("input#enterAddressPostalCode", profile.get("zip",""))
        page.fill("input#enterAddressPhoneNumber", profile.get("phone",""))
    except: pass

def purchase_walmart(product, account, qty):
    if not HAS_PLAYWRIGHT: return False
    profile = account["profile"]
    proxy = next_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        try:
            _post("Logging into Walmart...", "info")
            page.goto("https://www.walmart.com/account/login", wait_until="networkidle", timeout=30000)
            page.fill("input#email", account["email"])
            page.fill("input#password", account["password"])
            page.click("button[data-automation-id='signin-submit-btn']")
            page.wait_for_load_state("networkidle")

            # Add to cart with quantity (Walmart often uses API)
            sku = product["sku"]
            atc_url = f"https://www.walmart.com/checkout/fulfill/item/add?productId={sku}&quantity={qty}"
            page.goto(atc_url, wait_until="networkidle", timeout=15000)

            # Go to cart and checkout
            page.goto("https://www.walmart.com/cart", wait_until="networkidle")
            page.click("button:has-text('Checkout')")
            page.wait_for_load_state("networkidle")

            _fill_walmart_checkout(page, profile)
            if page.locator("[data-sitekey]").count():
                solve_captcha(page)
            page.click("button:has-text('Place order')")
            page.wait_for_timeout(10000)
            if "thank you" in page.content().lower():
                return True
            return False
        except Exception as e:
            _post(f"Walmart purchase error: {e}", "error")
            return False
        finally:
            browser.close()

def _fill_walmart_checkout(page, profile):
    try:
        page.fill("input[name='firstName']", profile.get("first_name",""))
        page.fill("input[name='lastName']", profile.get("last_name",""))
        page.fill("input[name='address1']", profile.get("address",""))
        page.fill("input[name='city']", profile.get("city",""))
        page.select_option("select[name='state']", profile.get("state",""))
        page.fill("input[name='zip']", profile.get("zip",""))
        page.fill("input[name='phone']", profile.get("phone",""))
    except: pass

# ── Main Doorbuster Scanner ─────────────────────────────────────────────────
def scan_doorbusters():
    for product in CFG["doorbusters"]:
        retailer = product["retailer"]
        name = product["name"]
        target = product["target_price"]
        max_qty = product.get("max_qty", 1)

        # Get current price
        if retailer == "bestbuy":
            current_price = get_current_price_bestbuy(product["sku"])
        elif retailer == "amazon":
            current_price = get_current_price_amazon(product["asin"])
        elif retailer == "walmart":
            current_price = get_current_price_walmart(product["sku"])
        else:
            continue

        if current_price is None:
            _post(f"{name} price unavailable.", "warning")
            continue

        # Determine how many already purchased for this product
        purchased_qty = sum(
            p.get("qty", 0) for p in STATE.get("purchased", [])
            if p.get("sku") == product.get("sku", product.get("asin", product.get("sku")))
        )

        if should_buy(current_price, target, max_qty, purchased_qty):
            qty_to_buy = max_qty - purchased_qty
            _post(f"🔥 DOORBUSTER! {name} at ${current_price} (target ${target}). "
                  f"Buying {qty_to_buy} (max {max_qty})", "error")
            success = False
            account = CFG.get("accounts", {}).get(retailer, {})
            if retailer == "bestbuy":
                success = purchase_bestbuy(product, account, qty_to_buy)
            elif retailer == "amazon":
                success = purchase_amazon(product, account, qty_to_buy)
            elif retailer == "walmart":
                success = purchase_walmart(product, account, qty_to_buy)

            if success:
                STATE.setdefault("purchased", []).append({
                    "sku": product.get("sku", product.get("asin", product.get("sku"))),
                    "name": name,
                    "price": current_price,
                    "qty": qty_to_buy,
                    "timestamp": datetime.utcnow().isoformat()
                })
                save_state(STATE)
                _post(f"Successfully purchased {qty_to_buy}x {name}!", "info")
                # List on eBay if enabled
                if CFG["ebay"].get("list_after_purchase"):
                    list_on_ebay(name, current_price)
            else:
                _post(f"Checkout failed for {name}. Will retry.", "warning")
        else:
            _post(f"{name}: ${current_price} (above ${target} or limit reached)", "info")

def main():
    wait_for_hub()
    _post("Doorbuster Arbitrage Bot online.", "info")
    while True:
        scan_doorbusters()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `doorbuster_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "accounts": {
    "bestbuy": {
      "email": "your@email.com",
      "password": "your_password",
      "profile": {
        "first_name": "John", "last_name": "Doe",
        "address": "123 Main St", "city": "New York", "state": "NY", "zip": "10001",
        "phone": "5551234567",
        "card_number": "4111111111111111", "card_expiry": "12/28", "card_cvv": "123"
      }
    },
    "amazon": {
      "email": "amazon@email.com",
      "password": "amazon_pass",
      "profile": {
        "first_name": "Jane", "last_name": "Doe",
        "address": "456 Oak Ave", "city": "New York", "state": "NY", "zip": "10001",
        "phone": "5559876543",
        "card_number": "5555555555554444", "card_expiry": "11/27", "card_cvv": "321"
      }
    },
    "walmart": {
      "email": "walmart@email.com",
      "password": "walmart_pass",
      "profile": {
        "first_name": "Mike", "last_name": "Smith",
        "address": "789 Pine Blvd", "city": "Bentonville", "state": "AR", "zip": "72712",
        "phone": "4795551234",
        "card_number": "378282246310005", "card_expiry": "10/26", "card_cvv": "123"
      }
    }
  },
  "doorbusters": [
    {
      "retailer": "bestbuy",
      "sku": "6521434",
      "name": "RTX 5090 FE",
      "url": "https://www.bestbuy.com/site/nvidia-geforce-rtx-5090-founders-edition/6521434.p?skuId=6521434",
      "target_price": 1499.99,
      "max_qty": 2
    },
    {
      "retailer": "amazon",
      "asin": "B0DXYZ",
      "name": "85\" Samsung QLED 8K",
      "url": "https://www.amazon.com/dp/B0DXYZ",
      "target_price": 2999.99,
      "max_qty": 1
    },
    {
      "retailer": "walmart",
      "sku": "555123456",
      "name": "HP Victus Gaming PC",
      "url": "https://www.walmart.com/ip/555123456",
      "target_price": 899.00,
      "max_qty": 2
    }
  ],
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"},
  "ebay": {
    "enabled": false,
    "markup_multiplier": 1.5,
    "list_after_purchase": true
  }
}
"""
