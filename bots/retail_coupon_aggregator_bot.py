#!/usr/bin/env python3
"""
retail_coupon_aggregator_bot.py — Coupon Code Tester
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Uses a real browser (Playwright) to navigate to a retailer's
cart page, apply a coupon code, and check if a discount appears.

═══════════════════ SETUP ═══════════════════════════════
1. Install dependencies:
      pip install requests playwright
      python -m playwright install chromium

2. Create a config file named `coupon_config.json` next to
   this script. Define your target site(s) and coupon codes.
   See the example at the bottom.

3. Attach to BotController as usual.
"""

import json
import os
import time
import random
import re
import requests
from datetime import datetime
from urllib.parse import urljoin

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "retail_coupon_aggregator_bot"
BOT_NAME = "Coupon Tester"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "coupon_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "coupon_state.json")

SCAN_INTERVAL      = 3600   # 1 hour between full rounds of testing
HEARTBEAT_INTERVAL = 20
_last_hb = 0.0

# ── Hub helpers ────────────────────────────────────────────────────────────────
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
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": "online"
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

def _wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Config & state ─────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        _post("Config file missing. Create coupon_config.json.", "error")
        return None
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"tested": {}, "last_run": None}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Coupon testing engine (Real browser) ──────────────────────────────────────
def test_coupon(site, coupon_code, config=None):
    """
    Navigate to the site's cart page, apply the coupon, and return
    (discount_amount, discount_description) or None if not valid.
    """
    if not HAS_PLAYWRIGHT:
        _post("Playwright not installed. Cannot test coupons.", "error")
        return None

    # Use site-specific config or fallback to global defaults
    cart_url = site.get("cart_url")
    code_input_selector = site.get("coupon_input_selector", "#coupon_code")
    apply_button_selector = site.get("apply_button_selector", "[name='apply_coupon']")
    success_selector = site.get("success_selector", ".woocommerce-message, .coupon-message, .discount-info")
    error_selector = site.get("error_selector", ".woocommerce-error, .coupon-error")
    discount_amount_selector = site.get("discount_amount_selector", ".discount-total .amount, .cart-discount .amount")
    cart_total_before_selector = site.get("cart_total_before_selector", ".cart-subtotal .amount")
    cart_total_after_selector = site.get("cart_total_after_selector", ".order-total .amount")

    # Optional: if site requires login, we'd need cookies or credentials (not implemented here)
    # This example assumes the cart is accessible without login, or session cookies are provided.

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1280, "height": 720},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()

        try:
            # 1. Go to cart page
            _post(f"Testing {coupon_code} on {site.get('name', cart_url)}...", "info")
            page.goto(cart_url, wait_until="networkidle", timeout=30000)
            # Small pause to let any dynamic content load
            page.wait_for_timeout(random.randint(1000, 3000))

            # 2. Enter coupon code
            # Check if coupon input exists
            if not page.locator(code_input_selector).count():
                _post(f"Coupon input field not found for {site.get('name')}. Check selector.", "warning")
                return None
            page.fill(code_input_selector, coupon_code)

            # 3. Click apply
            page.click(apply_button_selector)
            # Wait for response (either success or error message)
            try:
                # Wait for either success or error message to appear
                page.wait_for_selector(f"{success_selector}, {error_selector}",
                                       timeout=10000)
            except PlaywrightTimeout:
                _post(f"No success/error message after applying {coupon_code}", "warning")
                return None

            page.wait_for_timeout(1000)

            # 4. Check for error
            error_elem = page.locator(error_selector).first
            if error_elem.is_visible():
                err_text = error_elem.inner_text()
                _post(f"Coupon {coupon_code} rejected: {err_text.strip()}", "info")
                return None

            # 5. Success detected — extract discount
            success_elem = page.locator(success_selector).first
            if not success_elem.is_visible():
                # Sometimes the page updates totals without a visible message
                pass

            # Try to extract discount amount
            discount = None
            desc = None
            # Look for direct discount display
            if discount_amount_selector:
                disc_elem = page.locator(discount_amount_selector).first
                if disc_elem.is_visible():
                    text = disc_elem.inner_text()
                    discount = _extract_number(text)
            # Fallback: compare cart totals before and after (if we can)
            if discount is None:
                before = page.locator(cart_total_before_selector).first
                after = page.locator(cart_total_after_selector).first
                if before.is_visible() and after.is_visible():
                    before_price = _extract_number(before.inner_text())
                    after_price = _extract_number(after.inner_text())
                    if before_price and after_price:
                        discount = round(before_price - after_price, 2)
            # Get discount description from success message
            desc = success_elem.inner_text().strip() if success_elem.is_visible() else f"{coupon_code} applied"

            return discount, desc

        except Exception as e:
            _post(f"Error while testing {coupon_code}: {e}", "error")
            return None
        finally:
            browser.close()

def _extract_number(text):
    """Extracts first monetary number from a string like '$49.99'."""
    if not text:
        return None
    nums = re.findall(r'[\d,]+\.?\d{0,2}', text.replace(",", ""))
    if nums:
        return float(nums[0])
    return None

# ── Main scan ─────────────────────────────────────────────────────────────────
def scan():
    config = load_config()
    if not config:
        return
    sites = config.get("sites", [])
    if not sites:
        _post("No sites configured.", "warning")
        return

    state = load_state()
    # Rate limiting: wait random 30-60 seconds between sites
    for site in sites:
        if not site.get("cart_url"):
            _post(f"Site missing cart_url: {site}", "error")
            continue
        site_id = site.get("name", site["cart_url"])
        codes = site.get("coupons", [])
        for code in codes:
            key = f"{site_id}|{code}"
            if key in state.get("tested", {}):
                # Already tested, skip (or retry after X days? skip for now)
                continue

            # Test the code
            result = test_coupon(site, code)
            if result:
                discount, desc = result
                if discount and discount > 0:
                    _post(f"✅ WORKING: {code} → {desc} (${discount:.2f} off)",
                          "error", {"site": site_id, "code": code, "discount": discount})
                else:
                    _post(f"Coupon {code} applied but no discount detected ({desc})", "info")
            # Mark as tested regardless of outcome
            state.setdefault("tested", {})[key] = datetime.utcnow().isoformat()

            # Delay between tests to mimic human behavior
            delay = random.uniform(5, 15)
            _post(f"Waiting {delay:.0f}s before next code...", "info")
            time.sleep(delay)

        _heartbeat()
        # Delay between sites
        time.sleep(random.randint(30, 60))

    state["last_run"] = datetime.utcnow().isoformat()
    save_state(state)

def main():
    _wait_for_hub()
    if not HAS_PLAYWRIGHT:
        _post("Playwright is required. Install with: pip install playwright && python -m playwright install", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    _post("Coupon Tester online — testing codes in real browser.", "info")
    while True:
        try:
            scan()
        except Exception as e:
            _post(f"Scan error: {e}", "error")
        _heartbeat()
        time.sleep(SCAN_INTERVAL)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `coupon_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "sites": [
    {
      "name": "My Store",
      "cart_url": "https://www.example-store.com/cart/",
      "coupon_input_selector": "#coupon_code",
      "apply_button_selector": "[name='apply_coupon']",
      "success_selector": ".woocommerce-message",
      "error_selector": ".woocommerce-error",
      "discount_amount_selector": ".cart-discount .amount",
      "cart_total_before_selector": ".cart-subtotal .amount",
      "cart_total_after_selector": ".order-total .amount",
      "coupons": [
        "SAVE10",
        "FREESHIP2023",
        "WELCOME15"
      ]
    }
  ]
}
"""
