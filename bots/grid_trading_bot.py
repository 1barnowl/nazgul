#!/usr/bin/env python3
"""
grid_trading_bot.py — Grid Trading Bot (Real Binance Execution)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Places a grid of BUY/SELL limit orders on Binance (testnet or live)
to profit from sideways volatility.

Prerequisites:
    pip install python-binance requests

Environment variables (set these before running):
    BINANCE_API_KEY    = your-api-key
    BINANCE_API_SECRET = your-api-secret
    USE_BINANCE_LIVE   = true   (optional, defaults to testnet)

Without these the bot cannot place orders — it will just log a warning.
"""

import os
import time
import requests
from binance.client import Client
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_LIMIT
from binance.exceptions import BinanceAPIException

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "grid_trading_bot"
BOT_NAME = "Grid Trading Bot"

# ── Grid configuration ────────────────────────────────────────────────────────
SYMBOL        = "BTCUSDT"   # Binance pair (no hyphen)
GRID_LOW      = 60000.0     # lower bound (USDT)
GRID_HIGH     = 90000.0     # upper bound
GRID_LEVELS   = 7           # must be odd for symmetric placement
ORDER_QTY     = 0.0001      # BTC per order (tiny to start)
LOOKBACK      = 15          # seconds between price checks

# ── Binance client setup ──────────────────────────────────────────────────────
USE_LIVE = os.getenv("USE_BINANCE_LIVE", "false").lower() == "true"
API_KEY  = os.getenv("BINANCE_API_KEY")
SECRET   = os.getenv("BINANCE_API_SECRET")

if API_KEY and SECRET:
    if USE_LIVE:
        client = Client(API_KEY, SECRET)
        _post(f"Connected to Binance LIVE", "warning")  # will be logged later
    else:
        client = Client(API_KEY, SECRET, testnet=True)
        _post(f"Connected to Binance Testnet", "info")
else:
    client = None
    # Without keys we cannot trade – will post warnings at runtime.

# ── Hub helpers ────────────────────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id":   BOT_ID,
            "bot_name": BOT_NAME,
            "summary":  summary,
            "level":    level,
            "payload":  payload or {},
        }, timeout=5)
    except Exception:
        pass

def _heartbeat() -> None:
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME,
            "status":   "online" if client else "degraded",
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

# ── Grid engine ───────────────────────────────────────────────────────────────
class RealGridBot:
    def __init__(self):
        self.step = (GRID_HIGH - GRID_LOW) / (GRID_LEVELS - 1)
        self.open_orders = {}   # price -> binance orderId & side
        self.cancel_all_orders()  # clean slate

    def get_price(self):
        """Get live mid price from Binance ticker."""
        try:
            ticker = client.get_symbol_ticker(symbol=SYMBOL)
            return float(ticker["price"])
        except Exception:
            return None

    def cancel_all_orders(self):
        """Cancel any open orders on the symbol."""
        if not client:
            return
        try:
            orders = client.get_open_orders(symbol=SYMBOL)
            for o in orders:
                client.cancel_order(symbol=SYMBOL, orderId=o["orderId"])
                self.open_orders.pop(float(o["price"]), None)
                _post(f"Cancelled old order {o['orderId']} @ {o['price']}", "info")
            time.sleep(0.5)  # weight limit
        except BinanceAPIException as e:
            _post(f"Cancel error: {e.message}", "error")

    def initialize_grid(self, current_price):
        """Place initial buy orders below price, sell orders above."""
        if not client:
            return
        self.cancel_all_orders()
        for i in range(GRID_LEVELS):
            line = GRID_LOW + i * self.step
            if line < current_price - 1e-8:
                self._place_limit(SIDE_BUY, line)
            elif line > current_price + 1e-8:
                self._place_limit(SIDE_SELL, line)
        _post(f"Grid initialised @ {current_price:.2f} — {GRID_LEVELS} levels", "info")

    def _place_limit(self, side, price):
        """Place a limit order. Returns orderId or None."""
        try:
            order = client.create_order(
                symbol=SYMBOL,
                side=side,
                type=ORDER_TYPE_LIMIT,
                timeInForce="GTC",
                quantity=ORDER_QTY,
                price=str(round(price, 2)),
            )
            self.open_orders[price] = (order["orderId"], side)
            _post(f"Placed {side} limit @ {price:.2f}  (order {order['orderId']})", "info")
            return order["orderId"]
        except BinanceAPIException as e:
            _post(f"Order failed: {e.message}", "error")
            return None

    def _cancel_and_replace(self, price, old_side):
        """Cancel filled side and place opposite side at adjacent grid line."""
        if not client:
            return
        # Cancel any residual order at the same price (should be empty if filled)
        old_id = self.open_orders.pop(price, (None, None))[0]
        if old_id:
            try:
                client.cancel_order(symbol=SYMBOL, orderId=old_id)
            except:
                pass
        # Place opposite side at next grid line
        new_side = SIDE_SELL if old_side == SIDE_BUY else SIDE_BUY
        new_price = price + self.step if new_side == SIDE_SELL else price - self.step
        if GRID_LOW <= new_price <= GRID_HIGH:
            self._place_limit(new_side, new_price)

    def check_fills(self):
        """Poll open orders and see if any are filled; replace if so."""
        if not client:
            return
        try:
            orders = client.get_open_orders(symbol=SYMBOL)
        except BinanceAPIException as e:
            _post(f"Could not fetch open orders: {e.message}", "error")
            return

        still_open = set()
        for o in orders:
            still_open.add(float(o["price"]))

        # Detect fills: orders that were in our record but not in current open list
        filled_prices = []
        for price, (oid, side) in list(self.open_orders.items()):
            if price not in still_open:
                # Order probably filled
                _post(f"Order filled {oid} — {side} @ {price:.2f}", "info")
                filled_prices.append((price, side))
                # Replace with opposite side
                self._cancel_and_replace(price, side)

        # Update record with surviving orders
        self.open_orders = {float(o["price"]): (o["orderId"],
                             SIDE_BUY if o["side"] == "BUY" else SIDE_SELL)
                            for o in orders}

        if filled_prices:
            self._post_status()

    def _post_status(self):
        """Send account balance, P&L, open orders to dashboard."""
        try:
            price = self.get_price()
            acct = client.get_account()
            balances = {bal["asset"]: float(bal["free"]) for bal in acct["balances"]}
            btc = balances.get("BTC", 0)
            usdt = balances.get("USDT", 0)
            total_value = usdt + (btc * price)
            payload = {
                "price": price,
                "btc_bal": round(btc, 6),
                "usdt_bal": round(usdt, 2),
                "total_value": round(total_value, 2),
                "open_orders": {f"{p:.2f}": side for p, (_, side) in self.open_orders.items()}
            }
            _post(
                f"Balance: {usdt:.2f} USDT + {btc:.6f} BTC | "
                f"Value ≈ {total_value:.2f} USDT | Open orders: {len(self.open_orders)}",
                "info", payload
            )
        except BinanceAPIException as e:
            _post(f"Status error: {e.message}", "error")

    def run(self):
        """Main loop: check price, update grid, check fills."""
        price = self.get_price()
        if price is None:
            _post("Price fetch failed", "warning")
            return

        # If no open orders, initialise
        if not self.open_orders:
            self.initialize_grid(price)

        # Check for fills (periodically)
        self.check_fills()


# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()

    if not client:
        _post("⚠️ No Binance API keys set. Bot cannot trade. "
              "Export BINANCE_API_KEY and BINANCE_API_SECRET.", "error")
        while True:
            _heartbeat()
            time.sleep(60)

    bot = RealGridBot()

    _post(f"Grid Trading Bot started on {'LIVE' if USE_LIVE else 'TESTNET'}. "
          f"Range: {GRID_LOW}–{GRID_HIGH} USDT, {GRID_LEVELS} levels.", "info")

    while True:
        try:
            bot.run()
        except Exception as e:
            _post(f"Exception: {e}", "error")
        _heartbeat()
        time.sleep(LOOKBACK)


if __name__ == "__main__":
    main()
