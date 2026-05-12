#!/usr/bin/env python3
"""
region_phone_flipper_bot.py — International Phone Arbitrage Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. Scrapes international Amazon sites (DE, JP, UK, IN, etc.) for
   unlocked Samsung & Xiaomi flagships.
2. Compares total landed cost (item + shipping + import fees) to
   the current US market price (Amazon.com or eBay).
3. Alerts when profit exceeds a configurable threshold.
4. Optionally auto‑buys via Playwright and lists on eBay.

✦ FOR EDUCATIONAL / RESEARCH PURPOSES ONLY.
  Automated purchasing may violate Amazon’s Terms of Service.

SETUP
─────
1. Install dependencies:
      pip install playwright beautifulsoup4 requests ebaysdk
      python -m playwright install chromium

2. Set 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing (optional):
      export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `region_phone_config.json` (example at bottom).
   Fill in:
   - International Amazon accounts (optional; can buy as guest).
   - Payment / shipping details.
   - Target phone models with international ASINs.

5. Attach to BotController.
"""

import json, os, re, time, random, threading, requests
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import quote_plus

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
BOT_ID   = "region_phone_flipper_bot"
BOT_NAME = "Intl Phone Flipper"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "region_phone_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "region_phone_state.json")

SCAN_INTERVAL      = 7200   # 2 hours between full scans (costs data)
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
        "market_profile": {
            "us_tax_rate": 0.0,          # most states don't tax international
            "target_profit_percent": 15.0,
            "fixed_handling_fee": 15.0,
            "ebay_fvf_percent": 13.25    # Final Value Fee (varies)
        },
        "models": [
            {
                "name": "Samsung Galaxy S24 Ultra (Unlocked)",
                "us_market_price_url": "https://www.amazon.com/dp/B0CMDJXZ9D",
                "us_ebay_search": "Samsung Galaxy S24 Ultra unlocked",
                "variants": [
                    {
                        "region": "de",
                        "asin": "B0CMDK9B8M",
                        "url": "https://www.amazon.de/dp/B0CMDK9B8M",
                        "max_order_qty": 2
                    },
                    {
                        "region": "jp",
                        "asin": "B0CMDK9B8M",  # different ASIN on .co.jp
                        "url": "https://www.amazon.co.jp/dp/B0CMDK9B8M",
                        "max_order_qty": 1
                    }
                ]
            },
            {
                "name": "Xiaomi 14 Ultra (Global Unlocked)",
                "us_market_price_url": "https://www.amazon.com/dp/B0CWH2H9HS",
                "us_ebay_search": "Xiaomi 14 Ultra unlocked",
                "variants": [
                    {
                        "region": "de",
                        "asin": "B0CTJ8ZZ7Q",
                        "url": "https://www.amazon.de/dp/B0CTJ8ZZ7Q",
                        "max_order_qty": 2
                    }
                ]
            }
        ],
        "accounts": {
            "amazon_de": {
                "email": "de_account@example.com",
                "password": "password",
                "profile": {
                    "first_name": "Max", "last_name": "Mustermann",
                    "address": "Hauptstr. 1", "city": "Berlin", "state": "BE", "zip": "10115",
                    "phone": "+49123456789",
                    "card_number": "4111111111111111", "card_expiry": "12/28", "card_cvv": "123"
                }
            },
            "amazon_co_jp": {
                "email": "jp_account@example.com",
                "password": "password",
                "profile": {
                    "first_name": "Taro", "last_name": "Yamada",
                    "address": "1-1-1 Shinjuku", "city": "Tokyo", "state": "Tokyo", "zip": "160-0022",
                    "phone": "+819012345678",
                    "card_number": "5555555555554444", "card_expiry": "11/27", "card_cvv": "321"
                }
            }
        },
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {
            "enabled": False,
            "markup_multiplier": 1.3
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

# ── Price scraping (international Amazon) ───────────────────────────────────
def get_amazon_international_price(region, asin):
    """Return dict {price_eur, shipping_to_us, import_fees_estimate, total_usd} or None."""
    # Use Amazon's product API? We'll scrape the product page for price and use
    # a helper to calculate import fees based on known rates.
    # For simplicity, we'll parse the price in the local currency and convert to USD.
    # Shipping to US can be scraped from the "Ship to United States" section.
    url = f"https://www.amazon.{region}/dp/{asin}"
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200: return None
        soup = BeautifulSoup(resp.text, "html.parser")

        # Price extraction: Amazon uses various selectors (.a-price-whole, .a-price-fraction)
        price_whole = soup.select_one("span.a-price-whole")
        price_fraction = soup.select_one("span.a-price-fraction")
        if not price_whole: return None
        price_str = price_whole.get_text(strip=True).replace(",", "") + "." + (price_fraction.get_text(strip=True) if price_fraction else "00")
        price_local = float(price_str)

        # Currency mapping (simplified; real rates from API needed)
        currency_map = {"de": "EUR", "co.uk": "GBP", "co.jp": "JPY", "in": "INR", "fr": "EUR",
                        "it": "EUR", "es": "EUR", "ca": "CAD", "com.mx": "MXN"}
        currency = currency_map.get(region, "EUR")
        # Use a fixed conversion for demo; in production use live forex
        forex = {"EUR": 1.08, "GBP": 1.26, "JPY": 0.0067, "INR": 0.012, "CAD": 0.73, "MXN": 0.056}
        usd_rate = forex.get(currency, 1.0)
        price_usd = price_local * usd_rate

        # Shipping to US: check for "Ship to United States" section (often <div id="shippingMessage")
        shipping_text = soup.find("div", {"id": "shippingMessage"})
        shipping_usd = 15.0  # default estimate
        if shipping_text:
            text = shipping_text.get_text()
            # Attempt to parse shipping cost
            nums = re.findall(r'\$?([\d\.]+)', text)
            if nums:
                shipping_usd = float(nums[0])

        # Import fees: Amazon may show "Import Fees Deposit" on product page
        import_fees = 0.0
        import_elem = soup.find("span", text=re.compile("Import Fees Deposit"))
        if import_elem:
            fee_text = import_elem.parent.get_text()
            nums = re.findall(r'\$?([\d\.]+)', fee_text)
            if nums: import_fees = float(nums[0])

        total_usd = price_usd + shipping_usd + import_fees
        return {
            "price_local": price_local,
            "currency": currency,
            "price_usd": round(price_usd, 2),
            "shipping_usd": shipping_usd,
            "import_fees": import_fees,
            "total_usd": round(total_usd, 2)
        }
    except Exception as e:
        _post(f"Error scraping Amazon.{region} for ASIN {asin}: {e}", "warning")
        return None

def get_us_market_price_amazon(asin):
    """Get price from Amazon.com for the same ASIN (or search)."""
    url = f"https://www.amazon.com/dp/{asin}"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code != 200: return None
        soup = BeautifulSoup(resp.text, "html.parser")
        whole = soup.select_one("span.a-price-whole")
        fraction = soup.select_one("span.a-price-fraction")
        if whole:
            price_str = whole.get_text(strip=True).replace(",", "") + \
                        ("." + fraction.get_text(strip=True) if fraction else ".00")
            return float(price_str)
    except: pass
    return None

def get_us_market_price_ebay(keyword):
    """Optional: average completed listing price on eBay (skeleton)."""
    # Would require eBay Finding API; for now return None
    return None

# ── Profit calculation ─────────────────────────────────────────────────────
def calculate_profit(international_total_usd, us_market_price):
    if not us_market_price: return None
    fees = CFG["market_profile"]["ebay_fvf_percent"] / 100.0 * us_market_price
    cost = international_total_usd + fees + CFG["market_profile"]["fixed_handling_fee"]
    profit = us_market_price - cost
    profit_pct = (profit / cost) * 100 if cost > 0 else 0
    return {
        "cost": round(cost, 2),
        "revenue": round(us_market_price, 2),
        "profit": round(profit, 2),
        "profit_pct": round(profit_pct, 2)
    }

# ── Purchase flow (International Amazon) ───────────────────────────────────
def purchase_international(region, asin, qty, account):
    """Auto‑buy from Amazon.{region} using Playwright."""
    if not HAS_PLAYWRIGHT: return False
    proxy = next_proxy()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, proxy={"server": proxy} if proxy else None)
        page = browser.new_page()
        try:
            # Login
            _post(f"Logging into Amazon.{region}...", "info")
            page.goto(f"https://www.amazon.{region}/ap/signin", wait_until="networkidle", timeout=30000)
            page.fill("input#ap_email", account["email"])
            page.click("input#continue")
            page.wait_for_selector("input#ap_password", timeout=5000)
            page.fill("input#ap_password", account["password"])
            page.click("input#signInSubmit")
            page.wait_for_load_state("networkidle")

            # Add to cart
            page.goto(f"https://www.amazon.{region}/dp/{asin}", wait_until="networkidle")
            # Set quantity if needed (Amazon quantity dropdown often id=quantity)
            if qty > 1:
                try:
                    page.select_option("select#quantity", str(qty))
                except: pass
            page.click("input#add-to-cart-button")
            page.wait_for_timeout(2000)

            # Proceed to checkout with shipping to US
            page.goto(f"https://www.amazon.{region}/gp/cart/view.html", wait_until="networkidle")
            page.click("input[name='proceedToRetailCheckout']")
            page.wait_for_load_state("networkidle")

            # Shipping address: select the pre‑saved US address (or fill)
            # For a real bot, you'd need a US address defined in the account.
            # We assume the account has a valid US shipping address saved.
            page.click("a[href*='shipaddress']", timeout=10000)  # might need
            page.wait_for_timeout(1000)

            # Continue through payment
            page.click("input[name='placeYourOrder1']")
            page.wait_for_timeout(10000)
            if "thank you" in page.content().lower():
                return True
            return False
        except Exception as e:
            _post(f"Amazon.{region} purchase error: {e}", "error")
            return False
        finally:
            browser.close()

# ── eBay listing (standard) ───────────────────────────────────────────────
def list_on_ebay(model_name, purchase_price):
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
    title = f"{model_name} – Unlocked International Version – Fast US Shipping"
    payload = {
        "Item": {
            "Title": title[:80],
            "Description": f"Brand new, unlocked {model_name}. Imported, in hand ready to ship.",
            "PrimaryCategory": {"CategoryID": "9355"},
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
            _post(f"eBay listing created for {model_name} at ${ebay_price}", "info")
    except Exception as e: _post(f"eBay error: {e}", "error")

# ── Main arbitrage scanner ─────────────────────────────────────────────────
def scan_arbitrage():
    for model in CFG.get("models", []):
        name = model["name"]
        us_price = None
        # Try Amazon.com price if URL provided
        us_link = model.get("us_market_price_url")
        if us_link:
            asin_us = re.search(r'/dp/([A-Z0-9]+)', us_link)
            if asin_us:
                us_price = get_us_market_price_amazon(asin_us.group(1))
        if not us_price:
            # Fallback to eBay completed average (not implemented)
            _post(f"No US market price for {name}.", "warning")
            continue

        for variant in model.get("variants", []):
            region = variant["region"]
            asin = variant["asin"]
            # Check if already purchased recently
            key = f"{region}_{asin}"
            if any(p.get("key") == key for p in STATE.get("purchased", [])):
                continue
            intl_data = get_amazon_international_price(region, asin)
            if not intl_data:
                continue
            profit = calculate_profit(intl_data["total_usd"], us_price)
            if not profit or profit["profit_pct"] < CFG["market_profile"]["target_profit_percent"]:
                continue
            _post(f"💰 ARBITRAGE: {name} from {region.upper()} – "
                  f"Buy for ${intl_data['total_usd']}, sell for ${us_price} "
                  f"(profit ${profit['profit']} / {profit['profit_pct']}%)", "warning", profit)

            max_qty = variant.get("max_order_qty", 1)
            account = CFG.get("accounts", {}).get(f"amazon_{region}", None)
            if account and max_qty > 0:
                success = purchase_international(region, asin, max_qty, account)
                if success:
                    STATE.setdefault("purchased", []).append({"key": key, "timestamp": datetime.utcnow().isoformat()})
                    save_state(STATE)
                    if CFG["ebay"].get("enabled"):
                        list_on_ebay(name, us_price)

def main():
    wait_for_hub()
    _post("International Phone Flipper Bot online.", "info")
    while True:
        scan_arbitrage()
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()
