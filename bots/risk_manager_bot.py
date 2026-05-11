#!/usr/bin/env python3
"""
risk_manager_bot.py — Portfolio Circuit Breaker
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors total portfolio value from your exchange,
compares against the day's starting value, and triggers
a halt if the loss exceeds a configurable threshold.

SETUP
─────
1. Install dependencies:
      pip install ccxt requests

2. Set environment variables for your exchange:
      export EXCHANGE_API_KEY="your_key"
      export EXCHANGE_SECRET="your_secret"
   Replace EXCHANGE with the provider name (e.g. BINANCE).

3. Create a config file named `risk_manager_config.json`.
   (Example at the end of this file.)

   The bot will automatically record the portfolio value
   at UTC midnight as the daily reference. If the current
   value falls below (reference - threshold%), it trips.

4. Attach to BotController.
"""

import json
import os
import time
import logging
import requests
import ccxt
from datetime import datetime, timezone

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "risk_manager_bot"
BOT_NAME = "Risk Manager"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "risk_manager_config.json")
STATE_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "risk_manager_state.json")

HEARTBEAT_INTERVAL = 20
MONITOR_INTERVAL = 60          # seconds between balance checks
_last_hb = 0.0
circuit_breached = False       # global flag for the day

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
    status = "online"
    if circuit_breached:
        status = "degraded"
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME, "status": status
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

# ── Config helpers ─────────────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        _post("Config file missing. Creating default.", "error")
        default = {
            "exchange": "binance",
            "threshold_percent": 5.0,
            "halt_url": "",               # e.g. "http://localhost:8899/halt"
            "quote_currency": "USDT",      # what to measure portfolio value in
            "use_testnet": False
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def load_state():
    if not os.path.exists(STATE_FILE):
        return {"daily_ref": None, "circuit_breached": False}
    with open(STATE_FILE, "r") as f:
        return json.load(f)

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Exchange client ────────────────────────────────────────────────────────────
def init_exchange(cfg):
    exchange_name = cfg["exchange"]
    api_key = os.getenv(f"{exchange_name.upper()}_API_KEY")
    secret  = os.getenv(f"{exchange_name.upper()}_SECRET")
    if not api_key or not secret:
        _post(f"Missing API keys for {exchange_name}. Cannot monitor portfolio.", "error")
        return None
    exchange_class = getattr(ccxt, exchange_name)
    params = {
        'apiKey': api_key,
        'secret': secret,
        'enableRateLimit': True,
    }
    if cfg.get("use_testnet"):
        if exchange_name == 'binance':
            params['urls'] = {
                'api': {
                    'public': 'https://testnet.binance.vision/api/v3',
                    'private': 'https://testnet.binance.vision/api/v3',
                }
            }
    try:
        exchange = exchange_class(params)
        exchange.load_markets()
        _post(f"Connected to {exchange_name}.", "info")
        return exchange
    except Exception as e:
        _post(f"Failed to connect to {exchange_name}: {e}", "error")
        return None

# ── Portfolio valuation ────────────────────────────────────────────────────────
def total_portfolio_value(exchange, quote="USDT"):
    """
    Fetch balances and compute total value in the quote currency.
    For non‑quote assets, we use the latest price (USDT pairs assumed).
    Returns value in quote or None on failure.
    """
    try:
        balances = exchange.fetch_balance()
        total = 0.0
        for asset, data in balances['total'].items():
            amount = float(data)
            if amount == 0:
                continue
            if asset == quote:
                total += amount
            else:
                # Find a suitable market, e.g. BTC/USDT
                # CCXT markets are structured as BASE/QUOTE
                market = exchange.markets.get(f"{asset}/{quote}")
                if not market:
                    # Try inverse quote?
                    continue
                ticker = exchange.fetch_ticker(market['symbol'])
                price = ticker['last'] if ticker['last'] else ticker['close']
                if price:
                    total += amount * price
        return total
    except Exception as e:
        _post(f"Error computing portfolio value: {e}", "warning")
        return None

# ── Circuit breaker logic ─────────────────────────────────────────────────────
def check_and_enforce(cfg, exchange, state):
    global circuit_breached

    # If circuit already breached today, do nothing (keeps halted)
    if circuit_breached:
        return

    # Get current portfolio value
    current_val = total_portfolio_value(exchange, cfg.get("quote_currency", "USDT"))
    if current_val is None:
        return

    # Determine daily reference value
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    daily_ref = state.get("daily_ref")

    # If no ref or date has changed, reset
    if daily_ref is None or daily_ref.get("date") != today_str:
        daily_ref = {
            "date": today_str,
            "value": current_val
        }
        state["daily_ref"] = daily_ref
        state["circuit_breached"] = False
        save_state(state)
        _post(f"New day. Reference portfolio value: ${current_val:,.2f}", "info")
        return

    ref_value = daily_ref["value"]
    loss_pct = ((ref_value - current_val) / ref_value) * 100
    threshold = cfg["threshold_percent"]

    if loss_pct >= threshold:
        # BREACH!
        circuit_breached = True
        state["circuit_breached"] = True
        save_state(state)

        _post(
            f"🚨 CIRCUIT BREAKER TRIPPED! Loss {loss_pct:.2f}% exceeds {threshold}% limit. "
            f"Portfolio: ${current_val:,.2f} vs. start ${ref_value:,.2f}",
            "error",
            {"loss_pct": round(loss_pct, 2), "current_value": current_val, "ref_value": ref_value}
        )

        # Optional: Send halt command to other bots
        halt_url = cfg.get("halt_url", "").strip()
        if halt_url:
            try:
                requests.post(halt_url, json={"action": "halt", "reason": f"Loss limit {threshold}% breached"}, timeout=5)
                _post(f"Halt signal sent to {halt_url}", "warning")
            except Exception as e:
                _post(f"Failed to send halt signal: {e}", "error")
    else:
        # Normal status
        _post(f"Portfolio: ${current_val:,.2f} | change: {loss_pct:+.2f}% today", "info")

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    cfg = load_config()
    state = load_state()

    # Restore circuit breaker state from file (in case of restart)
    global circuit_breached
    if state.get("circuit_breached"):
        circuit_breached = True
        _post("Circuit breaker was previously tripped today. Trading remains halted.", "warning")

    exchange = init_exchange(cfg)
    if not exchange:
        _post("Exchange connection missing. Bot idle.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    _post(f"Risk Manager online. Day loss limit: {cfg['threshold_percent']}%.", "info")

    while True:
        check_and_enforce(cfg, exchange, state)
        _heartbeat()
        time.sleep(MONITOR_INTERVAL)

if __name__ == "__main__":
    main()

# ═══════════════════════════════════════════════════════════════════════════════
# Example `risk_manager_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "exchange": "binance",
  "threshold_percent": 5.0,
  "halt_url": "http://localhost:8899/halt",
  "quote_currency": "USDT",
  "use_testnet": false
}
"""
