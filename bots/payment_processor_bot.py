#!/usr/bin/env python3
"""
payment_processor_bot.py — Payment Processor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Carries out real crypto withdrawals and ACH payouts based
on configurable rules (affiliate commissions, etc.).

SETUP
─────
1. Install dependencies:
      pip install ccxt stripe requests

2. Create a config file named `payment_config.json`.
   See the example at the bottom.

3. Set environment variables for each provider you intend
   to use (crypto exchange and/or Stripe).

4. Attach to BotController.

   The bot can also receive instant payment instructions via
   POST /pay on port 8900 (localhost).
"""

import json
import os
import time
import hashlib
import threading
import requests
from datetime import datetime, timezone

import ccxt
import stripe

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "payment_processor_bot"
BOT_NAME = "Payment Processor"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "payment_config.json")

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

# ── Config loader ──────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        _post("Config file missing. Create payment_config.json.", "error")
        return None
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

# ── Payment engines ────────────────────────────────────────────────────────────

# ═══ Crypto withdrawals (via CCXT) ═══
def init_crypto_exchange(cfg):
    """Initialise crypto exchange from config."""
    provider = cfg.get("crypto_provider", "binance")
    exchange_class = getattr(ccxt, provider)
    api_key = os.getenv(f"{provider.upper()}_API_KEY")
    secret = os.getenv(f"{provider.upper()}_SECRET")
    if not api_key or not secret:
        _post(f"Missing API keys for {provider}. Crypto withdrawals disabled.", "warning")
        return None
    params = {
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
    }
    # testnet support – enable only if needed
    if cfg.get("crypto_testnet"):
        if provider == 'binance':
            params['urls'] = {
                'api': {
                    'public': 'https://testnet.binance.vision/api/v3',
                    'private': 'https://testnet.binance.vision/api/v3',
                }
            }
    try:
        exchange = exchange_class(params)
        exchange.load_markets()
        _post(f"Connected to {provider} for crypto payouts.", "info")
        return exchange
    except Exception as e:
        _post(f"Cannot init {provider}: {e}", "error")
        return None

def execute_crypto_payout(exchange, currency, amount, address, network=None):
    """
    Withdraw crypto from exchange to an external address.
    Returns (success: bool, message: str, txid: str or None).
    """
    try:
        # Some exchanges require the currency in their own format (e.g. 'USDT' not 'USDT-USDT')
        # We'll assume the currency string matches the exchange's market.
        # Withdrawal parameters vary; most support: currency, amount, address, tag, network
        params = {
            'currency': currency,
            'amount': amount,
            'address': address,
        }
        if network:
            params['network'] = network
        # Some exchanges also need a 'tag' for certain coins – omitted for simplicity
        result = exchange.withdraw(**params)
        _post(f"Withdrawal for {amount} {currency} to {address[:10]}... submitted. ID: {result.get('id')}", "info")
        return True, "Submitted", result.get('id')
    except Exception as e:
        msg = f"Withdrawal failed: {e}"
        _post(msg, "error")
        return False, msg, None

# ═══ ACH / Wire (via Stripe) ═══
def init_stripe(cfg):
    stripe_key = os.getenv("STRIPE_SECRET_KEY")
    if not stripe_key:
        _post("Missing STRIPE_SECRET_KEY. ACH payouts disabled.", "warning")
        return None
    stripe.api_key = stripe_key
    # Stripe requires the connected account or bank token.
    # The config should hold the destination bank_account token or connected account ID.
    return True  # simple flag

def execute_ach_payout(amount_usd, destination_token, description="Payment"):
    """
    Create an ACH payout via Stripe.
    destination_token can be a Stripe connected account ID (acct_xxx)
    or a bank account token (ba_xxx) for the platform.
    This example uses Stripe Connect: payout to a connected account.
    """
    try:
        # If destination_token looks like an account (acct_), we send a payout to it.
        if destination_token.startswith("acct_"):
            payout = stripe.Payout.create(
                amount=int(amount_usd * 100),  # cents
                currency="usd",
                description=description,
                destination=destination_token,
                method="standard",  # ACH
            )
        else:
            # Otherwise, assume it's a bank account token (ba_xxx) on the platform
            payout = stripe.Payout.create(
                amount=int(amount_usd * 100),
                currency="usd",
                description=description,
                destination=destination_token,
                method="standard",
            )
        _post(f"ACH payout of ${amount_usd:.2f} sent to {destination_token} (ID {payout.id})", "info")
        return True, "Submitted", payout.id
    except Exception as e:
        msg = f"Stripe payout failed: {e}"
        _post(msg, "error")
        return False, msg, None

# ── Rule evaluation & scheduled payments ─────────────────────────────────────
def process_rule(rule, exchange, stripe_ok):
    """
    Execute a payment rule.
    rule dict: {
        "trigger": "schedule" or "condition",
        "provider": "crypto" or "ach",
        "currency": "USDT",
        "amount": 100,
        "destination": "0x... or bank_token",
        "network": "TRX" (optional),
        "schedule": "0 0 * * 5" (cron-like) OR "interval_minutes": 1440
    }
    """
    provider = rule.get("provider")
    if provider == "crypto" and not exchange:
        _post("Crypto exchange not available, skipping rule.", "warning")
        return
    if provider == "ach" and not stripe_ok:
        _post("Stripe not available, skipping ACH rule.", "warning")
        return

    amount = float(rule["amount"])
    destination = rule["destination"]

    if provider == "crypto":
        network = rule.get("network")
        execute_crypto_payout(exchange, rule["currency"], amount, destination, network)
    else:  # ach
        execute_ach_payout(amount, destination, rule.get("description", "Payment"))

def should_trigger(rule, last_triggers):
    """Simple scheduler based on interval_minutes or a single daily time."""
    # For real cron, you'd need a scheduler library; here we use interval minutes or delay
    rule_id = hashlib.md5(json.dumps(rule, sort_keys=True).encode()).hexdigest()
    last_time = last_triggers.get(rule_id, 0)
    now = time.time()

    if "interval_minutes" in rule:
        interval_sec = rule["interval_minutes"] * 60
        if now - last_time >= interval_sec:
            last_triggers[rule_id] = now
            return True
    elif "schedule" in rule:
        # Very simple: if the schedule is a time like "12:00" we check if current hour and minute match
        # But we'll only do that if the last trigger was before today (once per day)
        # For demo: we'll just trigger if we didn't trigger today and current time matches pattern (we'll skip full cron)
        # Instead, use interval_minutes for now.
        pass
    return False

# ── HTTP receiver for instant payments (e.g., from other bots) ────────────────
from http.server import BaseHTTPRequestHandler, HTTPServer

class PaymentReceiver(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path == "/pay":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            # body: {"provider": "crypto", "currency": "USDT", "amount": 50, "destination": "0x...", "network": "ERC20"}
            provider = body.get("provider")
            if provider == "crypto" and crypto_exchange:
                execute_crypto_payout(crypto_exchange, body["currency"], body["amount"],
                                      body["destination"], body.get("network"))
            elif provider == "ach" and stripe_ok:
                execute_ach_payout(body["amount"], body["destination"], body.get("description"))
            else:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"Payment provider unavailable")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

def start_receiver(port=8900):
    srv = HTTPServer(("127.0.0.1", port), PaymentReceiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _post(f"Payment receiver listening on :{port}", "info")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    cfg = load_config()
    if not cfg:
        _post("Config missing. Creating a template.", "info")
        cfg = {
            "crypto_provider": "binance",
            "crypto_testnet": False,
            "rules": []
        }
        save_config(cfg)

    global crypto_exchange, stripe_ok
    crypto_exchange = init_crypto_exchange(cfg)
    stripe_ok = init_stripe(cfg)

    if not crypto_exchange and not stripe_ok:
        _post("No payment providers active. Bot idle.", "error")
    else:
        _post("Payment Processor ready. Watching rules and /pay endpoint.", "info")

    # Start the HTTP receiver for instant payments
    start_receiver()

    last_triggers = {}
    while True:
        cfg = load_config()
        if cfg and cfg.get("rules"):
            for rule in cfg["rules"]:
                if should_trigger(rule, last_triggers):
                    process_rule(rule, crypto_exchange, stripe_ok)
        _heartbeat()
        time.sleep(30)  # check every 30 seconds

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `payment_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "crypto_provider": "binance",
  "crypto_testnet": false,
  "rules": [
    {
      "name": "Weekly affiliate payout - John",
      "provider": "crypto",
      "currency": "USDT",
      "amount": 250,
      "destination": "TXYZ1234...",
      "network": "TRC20",
      "interval_minutes": 10080
    },
    {
      "name": "Monthly commission - Sarah",
      "provider": "ach",
      "amount": 500,
      "destination": "acct_1ABC...",
      "description": "Affiliate commission March 2026",
      "interval_minutes": 43200
    }
  ]
}
"""
