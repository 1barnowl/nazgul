#!/usr/bin/env python3
"""
spot_order_execution_bot.py — Spot Order Execution Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Places real market and limit orders on crypto (CCXT) and
stocks (Alpaca). Trades are triggered via messages to the
BotController hub, or from a configuration file.

SETUP
─────
1. Install dependencies:
      pip install ccxt requests alpaca-trade-api

2. Create a config file named `spot_order_config.json` next to
   this script. See the example at the bottom.

3. For crypto: set BINANCE_API_KEY / BINANCE_SECRET env vars.
   For stocks: set ALPACA_API_KEY / ALPACA_SECRET_KEY env vars.

4. Attach to BotController as usual.
"""

import json
import os
import time
import threading
import requests
import ccxt
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, LimitOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "spot_order_execution_bot"
BOT_NAME = "Spot Order Executor"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "spot_order_config.json")

HEARTBEAT_INTERVAL = 20
_last_hb = 0.0

# ── Account clients ────────────────────────────────────────────────────────────

# Crypto: Binance (default, change exchange as needed)
BINANCE_API_KEY    = os.getenv("BINANCE_API_KEY")
BINANCE_SECRET     = os.getenv("BINANCE_API_SECRET")
USE_TESTNET        = os.getenv("USE_BINANCE_TESTNET", "false").lower() == "true"

if BINANCE_API_KEY and BINANCE_SECRET:
    exchange_params = {
        'apiKey': BINANCE_API_KEY,
        'secret': BINANCE_SECRET,
        'enableRateLimit': True,
    }
    if USE_TESTNET:
        crypto_client = ccxt.binance({
            **exchange_params,
            'urls': {
                'api': {
                    'public': 'https://testnet.binance.vision/api/v3',
                    'private': 'https://testnet.binance.vision/api/v3',
                },
                'www': 'https://testnet.binance.vision',
                'doc': 'https://binance-docs.github.io/apidocs/testnet/en/',
            },
        })
    else:
        crypto_client = ccxt.binance(exchange_params)
    crypto_client.load_markets()
else:
    crypto_client = None

# Stocks: Alpaca
ALPACA_API_KEY    = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER      = os.getenv("ALPACA_PAPER", "true").lower() == "true"

if ALPACA_API_KEY and ALPACA_SECRET_KEY:
    trading_client = TradingClient(
        api_key=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        paper=ALPACA_PAPER
    )
else:
    trading_client = None

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
    if not crypto_client and not trading_client:
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

# ── Order execution engines ───────────────────────────────────────────────────

def place_crypto_order(symbol, side, amount, order_type, limit_price=None):
    """
    Place an order on the crypto exchange via CCXT.
    side: 'buy' or 'sell'
    order_type: 'market' or 'limit'
    """
    if not crypto_client:
        return {"error": "Crypto client not configured."}
    try:
        if order_type == 'market':
            order = crypto_client.create_order(
                symbol=symbol,
                type='market',
                side=side.lower(),
                amount=amount
            )
        elif order_type == 'limit' and limit_price:
            order = crypto_client.create_order(
                symbol=symbol,
                type='limit',
                side=side.lower(),
                amount=amount,
                price=limit_price
            )
        else:
            return {"error": "Invalid order type or missing limit price."}
        return order
    except Exception as e:
        return {"error": str(e)}

def place_stock_order(symbol, qty, side, order_type, limit_price=None):
    """
    Place an order on Alpaca (stocks).
    side: 'buy' or 'sell'
    order_type: 'market' or 'limit'
    """
    if not trading_client:
        return {"error": "Alpaca client not configured."}
    try:
        side_enum = OrderSide.BUY if side.lower() == 'buy' else OrderSide.SELL
        if order_type == 'market':
            order_data = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side_enum,
                time_in_force=TimeInForce.DAY
            )
        elif order_type == 'limit' and limit_price:
            order_data = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=side_enum,
                limit_price=limit_price,
                time_in_force=TimeInForce.DAY
            )
        else:
            return {"error": "Invalid order type or missing limit price."}
        order = trading_client.submit_order(order_data)
        return order
    except Exception as e:
        return {"error": str(e)}

# ── Execution parser ───────────────────────────────────────────────────────────

def execute_order(order_spec):
    """
    order_spec dict with keys:
      market: 'crypto' or 'stock'
      symbol: e.g. 'BTC/USDT' or 'AAPL'
      side: 'buy'/'sell'
      amount: float (crypto) or qty (stock, integer/float)
      order_type: 'market' or 'limit'
      limit_price: float (if limit)
    """
    market = order_spec.get("market", "crypto").lower()
    symbol = order_spec["symbol"]
    side = order_spec["side"]
    order_type = order_spec.get("order_type", "market").lower()
    limit_price = order_spec.get("limit_price")

    if market == "crypto":
        amount = order_spec["amount"]
        result = place_crypto_order(symbol, side, amount, order_type, limit_price)
    else:  # stock
        qty = order_spec.get("qty", order_spec.get("amount"))
        result = place_stock_order(symbol, qty, side, order_type, limit_price)

    if "error" in result:
        _post(f"Order failed: {result['error']}", "error", order_spec)
    else:
        _post(f"Order placed: {side.upper()} {symbol} {order_type}", "info", result)
    return result

# ── Internal server to receive orders ────────────────────────────────────────
# BotController hub can also receive orders via POST /execute, forwarding to this bot.

from http.server import BaseHTTPRequestHandler, HTTPServer

class OrderReceiver(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        if self.path == "/execute":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                result = execute_order(body)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
            except Exception as e:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
        else:
            self.send_response(404)
            self.end_headers()

def start_receiver(port=8899):
    srv = HTTPServer(("127.0.0.1", port), OrderReceiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    _post(f"Order receiver listening on :{port}", "info")
    return srv

# ── Scheduled execution thread ────────────────────────────────────────────────
def scheduled_executor(config):
    """Reads `trigger_orders` from config and executes them immediately."""
    if not config:
        return
    orders = config.get("trigger_orders", [])
    for o in orders:
        execute_order(o)
        time.sleep(1)

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    cfg = load_config()
    if not cfg:
        _post("No config file or empty. Bot idle.", "warning")
    else:
        # Notify about capabilities
        crypto_ok = crypto_client is not None
        stock_ok = trading_client is not None
        _post(f"Spot Order Executor ready. Crypto: {'ON' if crypto_ok else 'OFF'}, Stocks: {'ON' if stock_ok else 'OFF'}", "info")

    # Start the receiver so other bots or scripts can send orders
    start_receiver()

    while True:
        # Re-read config in case of changes
        cfg = load_config()
        if cfg:
            scheduled_executor(cfg)
        _heartbeat()
        time.sleep(10)  # fast polling for config changes, but don't spam orders

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `spot_order_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "trigger_orders": [
    {
      "market": "crypto",
      "symbol": "BTC/USDT",
      "side": "buy",
      "amount": 0.0001,
      "order_type": "market"
    },
    {
      "market": "stock",
      "symbol": "AAPL",
      "side": "sell",
      "qty": 1,
      "order_type": "limit",
      "limit_price": 210.0
    }
  ]
}
"""
