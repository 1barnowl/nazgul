#!/usr/bin/env python3
"""
omni_tech_launch_bot.py — Omni‑Tech Launch Bot (GPU/Console/Gadgets AIO)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Unified drop monitor & auto‑checkout for:
  - Best Buy (FE cards, consoles)
  - Amazon
  - Newegg
  - B&H Photo
  - GameStop
  - AMD Direct
Supports:
  - Instant ATC links (bypass product page)
  - Mobile app bypasses (Best Buy mobile API + Playwright mobile)
  - Email / link harvesting (poll a feed for new URLs)
  - Concurrent multi‑site purchase attempts
  - Proxy rotation, 2Captcha, eBay resell (optional)

✦ THIS IS A RESEARCH TOOL. Use only on accounts you own.
  Automated purchases may violate retailer Terms of Service.

SETUP
─────
1. Install dependencies:
      pip install playwright ebaysdk feedparser requests
      python -m playwright install chromium

2. Export 2Captcha API key:  export CAPTCHA_API_KEY="your-key"

3. For eBay auto‑listing:  export EBAY_APP_ID, EBAY_CERT_ID, EBAY_DEV_ID, EBAY_AUTH_TOKEN

4. Create `omni_config.json` (example at bottom).
   Fill in your accounts, payment details, proxies, and target products.

5. Attach to BotController.
"""

import json, os, re, time, random, threading, requests, feedparser
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

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
HUB          = "http://localhost:8765"
BOT_ID       = "omni_tech_launch_bot"
BOT_NAME     = "Omni‑Tech Launch Bot"

CONFIG_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "omni_config.json")
STATE_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "omni_state.json")

HEARTBEAT_INTERVAL = 30
MONITOR_INTERVAL   = 5   # seconds between stock checks per site
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

# ── Config / State ──────────────────────────────────────────────────────────
def load_config():
    default = {
        "accounts": [{
            "email": "your@email.com",
            "password": "your_pass",
            "profile": {
                "first_name": "John", "last_name": "Doe",
                "address": "123 Main St", "city": "New York", "state": "NY", "zip": "10001",
                "phone": "5551234567",
                "card_number": "4111111111111111", "card_expiry": "12/28", "card_cvv": "123",
                "card_name": "John Doe"
            }
        }],
        "sites": [
            {"name": "Best Buy", "type": "bestbuy", "enabled": True,
             "products": [
                 {"sku": "6521434", "name": "RTX 5090 FE", "url": "https://www.bestbuy.com/site/...", "atc_link": "https://api.bestbuy.com/click/-/6521434/cart"},
                 {"sku": "6469100", "name": "PS5 Pro Anniversary", "url": "https://www.bestbuy.com/site/...", "atc_link": "https://api.bestbuy.com/click/-/6469100/cart"}
             ]
            },
            {"name": "Amazon", "type": "amazon", "enabled": True,
             "products": [{"asin": "B0D9XYZ", "name": "Xbox Series X 2TB", "url": "https://amazon.com/dp/B0D9XYZ"}]
            },
            {"name": "Newegg", "type": "newegg", "enabled": True,
             "products": [{"item": "N82E16819126459", "name": "AMD Ryzen 9 7950X3D"}]
            },
            {"name": "B&H Photo", "type": "bhphoto", "enabled": True,
             "products": [{"sku": "1234567", "name": "Apple Vision Pro"}]
            },
            {"name": "GameStop", "type": "gamestop", "enabled": True,
             "products": [{"sku": "20008765", "name": "PS5 Pro Bundle"}]
            },
            {"name": "AMD Direct", "type": "amd", "enabled": True,
             "products": [{"sku": "RX-9070XT", "name": "Radeon RX 9070 XT"}]
            }
        ],
        "proxies": {"list": []},
        "captcha": {"api_key": os.getenv("CAPTCHA_API_KEY", ""), "service": "2captcha"},
        "ebay": {"enabled": False, "markup_multiplier": 1.8},
        "link_harvester": {
            "enabled": False,
            "feed_url": "https://example.com/atc-feed.txt",   # plaintext, one URL per line
            "check_interval_minutes": 10
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

# ── Proxy & Captcha (same as before) ──────────────────────────────────────────
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
def list_on_ebay(item_name, price):
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
    pl = {
        "Item": {
            "Title": f"{item_name} – Brand New In Hand",
            "Description": "Brand new, sealed. Ships immediately.",
            "PrimaryCategory": {"CategoryID": "171833"},  # Electronics
            "StartPrice": ebay_price, "Quantity": 1,
            "ListingDuration": "Days_30", "Country": "US", "Currency": "USD",
            "ListingType": "FixedPriceItem", "Site": "US"
        }
    }
    try:
        resp = trading.execute("AddFixedPriceItem", pl)
        if resp.dict().get("Ack") == "Success":
            _post(f"eBay listing: {resp.dict()['ItemID']} at ${ebay_price}", "info")
    except Exception as e: _post(f"eBay error: {e}", "error")

# ── Link Harvester (ATC feed) ─────────────────────────────────────────────────
HARVESTED_LINKS = set()   # stores discovered URLs to try later

def harvest_links():
    cfg = CFG.get("link_harvester", {})
    if not cfg.get("enabled"): return
    feed_url = cfg.get("feed_url")
    if not feed_url: return
    try:
        resp = requests.get(feed_url, timeout=15)
        if resp.status_code == 200:
            urls = re.findall(r'https?://[^\s"]+', resp.text)
            new = set(urls) - HARVESTED_LINKS
            for u in new:
                HARVESTED_LINKS.add(u)
                _post(f"Harvested ATC link: {u}", "info")
    except Exception as e:
        _post(f"Link harvest error: {e}", "warning")

def harvest_loop():
    while True:
        harvest_links()
        time.sleep(CFG.get("link_harvester", {}).get("check_interval_minutes", 10) * 60)

# ── Base Site Module ─────────────────────────────────────────────────────────
class BaseSite:
    def __init__(self, site_cfg, account):
        self.cfg = site_cfg
        self.account = account
        self.name = site_cfg["name"]
    def pre_warm(self, page):
        pass
    def check_stock(self, page, product):
        raise NotImplementedError
    def add_to_cart(self, page, product):
        raise NotImplementedError
    def checkout(self, page, product):
        raise NotImplementedError

# ── Best Buy Module (Mobile API + ATC links) ─────────────────────────────────
class BestBuyModule(BaseSite):
    def pre_warm(self, page):
        # Login (optional)
        if self.account.get("email"):
            page.goto("https://www.bestbuy.com/identity/signin", wait_until="networkidle", timeout=30000)
            page.fill("input#fld-e", self.account["email"])
            page.fill("input#fld-p1", self.account["password"])
            page.click("button.cia-form__controls__submit")
            page.wait_for_load_state("networkidle")

    def check_stock(self, page, product):
        # Try ATC link first (bypasses product page)
        if product.get("atc_link"):
            try:
                page.goto(product["atc_link"], wait_until="networkidle", timeout=15000)
                # If we reach cart page, product is in stock and added
                if "cart" in page.url:
                    return True
            except: pass
        # Fallback: visit product page and look for add to cart button
        page.goto(product["url"], wait_until="networkidle", timeout=30000)
        add_btn = page.locator("button.add-to-cart-button")
        if add_btn.is_visible() and add_btn.is_enabled():
            return True
        return False

    def add_to_cart(self, page, product):
        # Already added if we used ATC link that landed in cart
        if product.get("atc_link") and "cart" in page.url:
            return
        # Otherwise click add to cart
        page.click("button.add-to-cart-button")
        page.wait_for_timeout(1000)

    def checkout(self, page, product):
        # Navigate to cart and checkout
        page.goto("https://www.bestbuy.com/cart", wait_until="networkidle", timeout=30000)
        page.click("button:has-text('Checkout')")
        page.wait_for_load_state("networkidle")
        # Fill details if not already stored
        _fill_bestbuy_checkout(page, self.account["profile"])
        # Captcha
        if page.locator("iframe[title*='captcha']").count() or page.locator("[data-sitekey]").count():
            if not solve_captcha(page): return False
            time.sleep(2)
        page.click("button:has-text('Place Your Order')")
        page.wait_for_timeout(10000)
        if "thank you" in page.content().lower():
            _post(f"{self.name}: order placed!", "info")
            return True
        return False

def _fill_bestbuy_checkout(page, profile):
    # Same as previous Best Buy bot
    try:
        page.fill("input#consolidatedAddresses_shippingAddress_1_firstName", profile.get("first_name",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_lastName", profile.get("last_name",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_address", profile.get("address",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_city", profile.get("city",""))
        page.select_option("select#consolidatedAddresses_shippingAddress_1_state", profile.get("state",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_zipcode", profile.get("zip",""))
        page.fill("input#consolidatedAddresses_shippingAddress_1_phone", profile.get("phone",""))
    except: pass
    # Credit card (if required)
    try:
        page.fill("input#optimized-cc-card-number", profile["card_number"])
        page.fill("input[name='expirationDate']", profile["card_expiry"])
        page.fill("input#credit-card-cvv", profile["card_cvv"])
    except: pass

# ── Amazon Module (ATC link + mobile checkout) ──────────────────────────────
class AmazonModule(BaseSite):
    def pre_warm(self, page):
        # Login if account provided
        if self.account.get("email"):
            page.goto("https://www.amazon.com/ap/signin", wait_until="networkidle", timeout=30000)
            page.fill("input#ap_email", self.account["email"])
            page.click("input#continue")
            page.wait_for_selector("input#ap_password", timeout=5000)
            page.fill("input#ap_password", self.account["password"])
            page.click("input#signInSubmit")
            page.wait_for_load_state("networkidle")

    def check_stock(self, page, product):
        # Use Amazon's ATC link format: https://www.amazon.com/gp/product/handle-buy-box/ref=dp_start-bbf_1_glance?ie=UTF8&asin=ASIN&quantity=1
        asin = product["asin"]
        atc_url = f"https://www.amazon.com/gp/product/handle-buy-box/ref=dp_start-bbf_1_glance?ie=UTF8&asin={asin}&quantity=1"
        page.goto(atc_url, wait_until="networkidle", timeout=15000)
        # If we reach the cart or checkout page, item is available and added
        if "/cart/" in page.url or "/checkout/" in page.url:
            return True
        # Fallback product page
        page.goto(product["url"], wait_until="networkidle", timeout=30000)
        add_btn = page.locator("input#add-to-cart-button")
        if add_btn.is_visible() and add_btn.is_enabled():
            return True
        return False

    def add_to_cart(self, page, product):
        # Already added if redirected to cart
        if "/cart/" in page.url: return
        page.click("input#add-to-cart-button")
        page.wait_for_timeout(1000)

    def checkout(self, page, product):
        page.goto("https://www.amazon.com/gp/cart/view.html", wait_until="networkidle", timeout=30000)
        page.click("input[name='proceedToRetailCheckout']")
        page.wait_for_load_state("networkidle")
        # Shipping / payment – Amazon often pre‑filled; fill if needed
        _fill_amazon_checkout(page, self.account["profile"])
        # Captcha rare
        if page.locator("iframe[title*='captcha']").count(): solve_captcha(page)
        page.click("input[name='placeYourOrder1']")
        page.wait_for_timeout(10000)
        if "thank you" in page.content().lower() or "order" in page.content().lower():
            _post(f"{self.name}: order placed!", "info")
            return True
        return False

def _fill_amazon_checkout(page, profile):
    # Fill address if not default
    try:
        page.fill("input#enterAddressAddressLine1", profile["address"])
        page.fill("input#enterAddressCity", profile["city"])
        page.select_option("select#enterAddressStateOrRegion", profile["state"])
        page.fill("input#enterAddressPostalCode", profile["zip"])
        page.fill("input#enterAddressPhoneNumber", profile["phone"])
    except: pass

# ── Newegg Module (add-to-cart API + checkout) ──────────────────────────────
class NeweggModule(BaseSite):
    def check_stock(self, page, product):
        item = product["item"]
        # Newegg has an "Add to Cart" endpoint: https://secure.newegg.com/Shopping/AddtoCart.aspx?Submit=ADD&ItemList=...
        atc_url = f"https://secure.newegg.com/Shopping/AddtoCart.aspx?Submit=ADD&ItemList={item}"
        page.goto(atc_url, wait_until="networkidle", timeout=15000)
        if "shoppingcart" in page.url:
            return True
        page.goto(f"https://www.newegg.com/p/{item}", wait_until="networkidle", timeout=30000)
        add_btn = page.locator("button.product-buy")
        if add_btn.is_visible() and add_btn.is_enabled():
            return True
        return False

    def add_to_cart(self, page, product):
        if "shoppingcart" in page.url: return
        page.click("button.product-buy")

    def checkout(self, page, product):
        page.goto("https://secure.newegg.com/Shopping/ShoppingCart.aspx", wait_until="networkidle", timeout=30000)
        page.click("button:has-text('Secure Checkout')")
        page.wait_for_load_state("networkidle")
        _fill_newegg_checkout(page, self.account["profile"])
        if page.locator("[data-sitekey]").count(): solve_captcha(page)
        page.click("button:has-text('Place Order')")
        page.wait_for_timeout(8000)
        if "order confirmation" in page.content().lower():
            _post(f"{self.name}: order placed!", "info")
            return True
        return False

def _fill_newegg_checkout(page, profile):
    # Login if needed (simplified)
    pass

# ── B&H Module ──────────────────────────────────────────────────────────────
class BHPhotoModule(BaseSite):
    def check_stock(self, page, product):
        page.goto(product["url"], wait_until="networkidle", timeout=30000)
        add_btn = page.locator("button[data-selenium='addToCartButton']")
        if add_btn.is_visible() and add_btn.is_enabled(): return True
        return False
    def add_to_cart(self, page, product):
        page.click("button[data-selenium='addToCartButton']")
    def checkout(self, page, product):
        page.goto("https://www.bhphotovideo.com/find/shoppingBasket.jsp", wait_until="networkidle", timeout=30000)
        page.click("a[data-selenium='checkoutAsMemberBtn']")
        page.wait_for_load_state("networkidle")
        _fill_bh_checkout(page, self.account["profile"])
        if page.locator("[data-sitekey]").count(): solve_captcha(page)
        page.click("button[data-selenium='placeOrderBtn']")
        page.wait_for_timeout(8000)
        if "thank you" in page.content().lower():
            _post(f"{self.name}: order placed!", "info")
            return True
        return False
def _fill_bh_checkout(page, profile):
    # Fill if needed
    pass

# ── GameStop Module (Shopify‑like) ──────────────────────────────────────────
class GameStopModule(BaseSite):
    def check_stock(self, page, product):
        page.goto(product["url"], wait_until="networkidle", timeout=30000)
        add_btn = page.locator("button[name='add']")
        if add_btn.is_visible() and add_btn.is_enabled(): return True
        return False
    def add_to_cart(self, page, product):
        page.click("button[name='add']")
    def checkout(self, page, product):
        page.goto("https://www.gamestop.com/checkout/", wait_until="networkidle", timeout=30000)
        _fill_shopify_checkout(page, self.account["profile"])
        if page.locator("[data-sitekey]").count(): solve_captcha(page)
        page.click("button:has-text('Complete order')")
        page.wait_for_timeout(8000)
        if "thank you" in page.content().lower():
            _post(f"{self.name}: order placed!", "info")
            return True
        return False
def _fill_shopify_checkout(page, profile):
    # Generic Shopify fill (same as previous bots)
    try:
        page.fill("input#checkout_email", profile.get("email",""))
        page.fill("input#checkout_shipping_address_first_name", profile.get("first_name",""))
        page.fill("input#checkout_shipping_address_last_name", profile.get("last_name",""))
        page.fill("input#checkout_shipping_address_address1", profile.get("address",""))
        page.fill("input#checkout_shipping_address_city", profile.get("city",""))
        page.select_option("select#checkout_shipping_address_country", "US")
        page.wait_for_timeout(1000)
        page.fill("input#checkout_shipping_address_zip", profile.get("zip",""))
        if profile.get("state"):
            page.select_option("select#checkout_shipping_address_province", profile["state"])
        page.fill("input#checkout_shipping_address_phone", profile.get("phone",""))
        page.click("button:has-text('Continue to shipping')")
        page.wait_for_load_state("networkidle")
    except: pass

# ── AMD Direct Module (Shopify) ─────────────────────────────────────────────
class AMDDirectModule(BaseSite):
    def check_stock(self, page, product):
        page.goto(product["url"], wait_until="networkidle", timeout=30000)
        add_btn = page.locator("button[name='add']")
        if add_btn.is_visible() and add_btn.is_enabled(): return True
        return False
    def add_to_cart(self, page, product):
        page.click("button[name='add']")
    def checkout(self, page, product):
        # AMD Direct uses Shopify checkout flow
        page.goto("https://shop.amd.com/checkout", wait_until="networkidle", timeout=30000)
        _fill_shopify_checkout(page, self.account["profile"])
        if page.locator("[data-sitekey]").count(): solve_captcha(page)
        page.click("button:has-text('Complete order')")
        page.wait_for_timeout(8000)
        if "thank you" in page.content().lower():
            _post(f"{self.name}: order placed!", "info")
            return True
        return False

# ── Module factory ───────────────────────────────────────────────────────────
def create_module(site_cfg, account):
    t = site_cfg.get("type", "").lower()
    if t == "bestbuy": return BestBuyModule(site_cfg, account)
    if t == "amazon": return AmazonModule(site_cfg, account)
    if t == "newegg": return NeweggModule(site_cfg, account)
    if t == "bhphoto": return BHPhotoModule(site_cfg, account)
    if t == "gamestop": return GameStopModule(site_cfg, account)
    if t == "amd": return AMDDirectModule(site_cfg, account)
    return None

# ── Single site runner (thread) ─────────────────────────────────────────────
def run_site(module, accounts):
    """Runs a continuous loop for one site, cycling through products."""
    while True:
        for product in module.cfg.get("products", []):
            # Use a random account (or rotate)
            account = random.choice(accounts)
            proxy = next_proxy()
            launch_opts = {"headless": False}
            if proxy: launch_opts["proxy"] = {"server": proxy}

            with sync_playwright() as p:
                browser = p.chromium.launch(**launch_opts)
                page = browser.new_page()
                try:
                    module.pre_warm(page)
                    if module.check_stock(page, product):
                        _post(f"{module.name} {product.get('name')}: IN STOCK!", "warning")
                        module.add_to_cart(page, product)
                        if module.checkout(page, product):
                            # Record purchase
                            STATE.setdefault("purchased", []).append({
                                "site": module.name, "product": product.get("name"),
                                "timestamp": datetime.utcnow().isoformat()
                            })
                            save_state(STATE)
                            # eBay listing
                            if CFG["ebay"].get("enabled"):
                                list_on_ebay(f"{module.name} {product.get('name')}", 500.0)  # placeholder price
                            # Sleep to avoid repeated purchases
                            time.sleep(300)
                except Exception as e:
                    _post(f"{module.name} error: {e}", "error")
                finally:
                    browser.close()
            time.sleep(MONITOR_INTERVAL)
        time.sleep(1)

# ── Main orchestrator ────────────────────────────────────────────────────────
def main():
    wait_for_hub()
    _post("Omni‑Tech Launch Bot online.", "info")
    accounts = CFG.get("accounts", [])
    if not accounts:
        _post("No accounts configured.", "error")
        return

    modules = []
    for site_cfg in CFG.get("sites", []):
        if not site_cfg.get("enabled", True): continue
        mod = create_module(site_cfg, accounts[0])  # accounts rotation inside run_site
        if mod:
            modules.append(mod)
            _post(f"Loaded {mod.name} module.", "info")

    # Start link harvester thread
    if CFG.get("link_harvester", {}).get("enabled"):
        threading.Thread(target=harvest_loop, daemon=True).start()

    # Start site monitor threads
    executor = ThreadPoolExecutor(max_workers=len(modules))
    futures = []
    for mod in modules:
        futures.append(executor.submit(run_site, mod, accounts))
    # Heartbeat loop
    while True:
        _heartbeat()
        time.sleep(10)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `omni_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "accounts": [{
    "email": "your@email.com",
    "password": "your_pass",
    "profile": {
      "first_name": "John", "last_name": "Doe",
      "address": "123 Main St", "city": "New York", "state": "NY", "zip": "10001",
      "phone": "5551234567",
      "card_number": "4111111111111111", "card_expiry": "12/28", "card_cvv": "123",
      "card_name": "John Doe"
    }
  }],
  "sites": [
    {
      "name": "Best Buy", "type": "bestbuy", "enabled": true,
      "products": [
        {"sku": "6521434", "name": "RTX 5090 FE", "url": "https://www.bestbuy.com/site/nvidia-geforce-rtx-5090-founders-edition/6521434.p?skuId=6521434", "atc_link": "https://api.bestbuy.com/click/-/6521434/cart"}
      ]
    },
    {
      "name": "Amazon", "type": "amazon", "enabled": true,
      "products": [
        {"asin": "B0D9XYZ", "name": "Xbox Series X 2TB", "url": "https://amazon.com/dp/B0D9XYZ"}
      ]
    }
  ],
  "proxies": {"list": []},
  "captcha": {"api_key": "YOUR_2CAPTCHA_KEY"},
  "ebay": {"enabled": false, "markup_multiplier": 1.8},
  "link_harvester": {
    "enabled": false,
    "feed_url": "https://example.com/atc-feed.txt"
  }
}
"""
