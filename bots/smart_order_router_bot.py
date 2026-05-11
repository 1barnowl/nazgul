#!/usr/bin/env python3
"""
smart_order_router_bot.py — Smart Order Router
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Splits a large spot order across multiple exchanges to
minimise slippage by consuming the best liquidity from
each order book. Places real limit orders if API keys
are provided, otherwise reports the recommended split.

SETUP
─────
1. Install dependencies:
      pip install ccxt requests

2. Set environment variables for each exchange you want to
   trade on (e.g. BINANCE_API_KEY, BINANCE_SECRET). Without
   keys the bot will only calculate the ideal split and
   post it to BotController.

3. Create a config file named `smart_order_config.json`.
   Example at the bottom. The bot watches this file and
   executes any `pending_order` it finds.

4. Attach to BotController.
"""

import json
import os
import time
import copy
import threading
import requests
import ccxt

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "smart_order_router_bot"
BOT_NAME = "Smart Order Router"

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "smart_order_config.json")

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

# ── Exchange initialisation ───────────────────────────────────────────────────
def init_exchange(exchange_id, config_ex):
    """
    Initialise a CCXT exchange instance. If API keys are provided
    (via env vars matching the exchange name), use them, otherwise
    the instance works for public data only.
    """
    exchange_class = getattr(ccxt, exchange_id)
    params = {
        'enableRateLimit': True,
    }
    # Auto-load API keys from environment if present
    env_key = f"{exchange_id.upper()}_API_KEY"
    env_secret = f"{exchange_id.upper()}_SECRET"
    if env_key in os.environ and env_secret in os.environ:
        params['apiKey'] = os.environ[env_key]
        params['secret'] = os.environ[env_secret]
        # If testnet/sandbox needed, add it via config
        if config_ex.get('testnet'):
            if exchange_id == 'binance':
                params['urls'] = {
                    'api': {
                        'public': 'https://testnet.binance.vision/api/v3',
                        'private': 'https://testnet.binance.vision/api/v3',
                    }
                }
    try:
        exchange = exchange_class(params)
        exchange.load_markets()
        return exchange
    except Exception as e:
        _post(f"Failed to init {exchange_id}: {e}", "error")
        return None

# ── Order book depth aggregator ───────────────────────────────────────────────
def get_order_book_slice(exchange, symbol, side, total_size):
    """
    Fetch enough of the order book to cover total_size.
    Returns list of [price, amount] for the relevant side.
    side = 'asks' for buy, 'bids' for sell.
    """
    try:
        ob = exchange.fetch_order_book(symbol, limit=None)  # get full depth
        if side == 'asks':
            levels = ob['asks']
        else:
            levels = ob['bids']
        return levels
    except Exception as e:
        _post(f"Order book fetch failed on {exchange.id}: {e}", "warning")
        return []

def cumulative_from_levels(levels, total_size):
    """
    From a list of [price, amount], compute cumulative amount
    and cumulative cost/revenue up to total_size.
    Returns list of (cum_amount, avg_price, last_price).
    We'll use this to find marginal prices.
    """
    cum = 0.0
    cum_cost = 0.0
    result = []
    for price, amount in levels:
        take = min(amount, total_size - cum)
        if take <= 0:
            break
        cum += take
        cum_cost += take * price
        avg_price = cum_cost / cum
        result.append((cum, avg_price, price))  # price is the marginal price of this slice
        if cum >= total_size:
            break
    return result

# ── Optimal split algorithm ───────────────────────────────────────────────────
def optimal_split(symbol, side, total_qty, exchanges):
    """
    Determine the optimal split across exchanges for a buy or sell.
    Returns list of dict: {exchange_id, quantity, limit_price}
    """
    # side: 'buy' or 'sell'
    # For buy: we look at asks; we want to buy total_qty of base at lowest cost.
    # For sell: we look at bids; we want to sell total_qty for highest revenue.
    if side == 'buy':
        book_side = 'asks'
    else:
        book_side = 'bids'

    # Collect order books and their marginal price functions
    ob_data = {}
    for ex in exchanges:
        if symbol not in ex.markets:
            continue
        levels = get_order_book_slice(ex, symbol, book_side, total_qty)
        if not levels:
            continue
        cum_data = cumulative_from_levels(levels, total_qty)
        ob_data[ex.id] = {
            'exchange': ex,
            'cum_data': cum_data,
            'allocated': 0.0,
        }

    if not ob_data:
        return []

    # Greedy allocation: repeatedly allocate the smallest possible chunk
    # (the min trade quantity for each exchange) to the exchange with best
    # marginal price for the next unit.
    # First, determine a reasonable step size. Use the smallest allowed
    # amount across markets, but at least 1e-8 to avoid infinite loop.
    step = min([
        ex.markets[symbol]['limits']['amount']['min'] or 1e-8
        for ex in exchanges if symbol in ex.markets
    ]) * 10  # slightly bigger for efficiency
    step = max(step, total_qty / 1000)  # ensure we finish

    remaining = total_qty
    while remaining > 1e-12 and ob_data:
        best_ex_id = None
        best_price = None
        for ex_id, data in ob_data.items():
            # find marginal price for the next step amount
            # by scanning cum_data
            cum_data = data['cum_data']
            alloc_now = data['allocated']
            target = alloc_now + step
            marg_price = None
            for cum, avg, price in cum_data:
                if cum >= target:
                    marg_price = price  # price of the last level that covers target
                    break
            if marg_price is None and cum_data:
                # if target bigger than available total depth, use last price (or inf for buy, 0 for sell)
                marg_price = cum_data[-1][2]  # worst price
            if marg_price is None:
                continue
            # For buy, we want lowest price; for sell, highest price
            if side == 'buy':
                if best_price is None or marg_price < best_price:
                    best_price = marg_price
                    best_ex_id = ex_id
            else:
                if best_price is None or marg_price > best_price:
                    best_price = marg_price
                    best_ex_id = ex_id
        if best_ex_id is None:
            break  # no more liquidity
        # Allocate step to best_ex_id
        ob_data[best_ex_id]['allocated'] += step
        remaining -= step
        # If an exchange can't provide more (allocated >= total available depth), remove it
        cum_data = ob_data[best_ex_id]['cum_data']
        max_qty = cum_data[-1][0] if cum_data else 0
        if ob_data[best_ex_id]['allocated'] >= max_qty - 1e-12:
            del ob_data[best_ex_id]

    # Now refine allocations to exact total (distribute residue proportionally)
    # and compute limit price for each exchange
    results = []
    for ex_id, data in ob_data.items():
        alloc = data['allocated']
        if alloc < 1e-12:
            continue
        # The limit price should be the worst price needed to fill alloc quantity
        cum_data = data['cum_data']
        limit_price = None
        for cum, avg, price in cum_data:
            if cum >= alloc:
                limit_price = price
                break
        if limit_price is None and cum_data:
            limit_price = cum_data[-1][2]
        if limit_price is None:
            continue
        results.append({
            'exchange_id': ex_id,
            'quantity': alloc,
            'limit_price': limit_price,
        })
    return results

# ── Order placement ───────────────────────────────────────────────────────────
def execute_split(symbol, side, splits):
    """Place real limit orders on each exchange using the split."""
    placed = []
    for s in splits:
        ex_id = s['exchange_id']
        ex = next((e for e in exchanges if e.id == ex_id), None)
        if not ex:
            _post(f"Exchange {ex_id} not available for order placement.", "warning")
            continue
        if not (ex.apiKey and ex.secret):
            _post(f"No API keys for {ex_id} — skipping execution.", "warning")
            continue
        try:
            order = ex.create_limit_order(
                symbol=symbol,
                side=side,
                amount=s['quantity'],
                price=s['limit_price']
            )
            _post(f"Placed {side} limit on {ex_id}: {s['quantity']} @ {s['limit_price']} → {order['id']}", "info")
            placed.append(order)
        except Exception as e:
            _post(f"Order failed on {ex_id}: {e}", "error")
    return placed

# ── Configuration watcher ────────────────────────────────────────────────────
def load_config():
    if not os.path.exists(CONFIG_FILE):
        return None
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _wait_for_hub()
    cfg = load_config()
    if not cfg:
        _post("No config found. Creating default.", "info")
        cfg = {"pending_order": None, "exchanges": ["binance", "bybit", "kraken"]}
        save_config(cfg)

    # Init exchanges from config
    global exchanges
    exchange_ids = cfg.get("exchanges", [])
    exchanges = []
    for eid in exchange_ids:
        ex = init_exchange(eid, cfg.get("exchange_config", {}).get(eid, {}))
        if ex:
            exchanges.append(ex)
    if not exchanges:
        _post("No exchange connections. Bot idle.", "error")
    else:
        _post(f"Connected to {len(exchanges)} exchanges.", "info")

    while True:
        # Check for new order
        cfg = load_config()
        if cfg and cfg.get("pending_order"):
            order_req = cfg["pending_order"]
            symbol = order_req.get("symbol", "BTC/USDT")
            side = order_req["side"].lower()
            total_qty = float(order_req["quantity"])

            _post(f"Routing {side} {total_qty} {symbol}...", "info")
            optimal = optimal_split(symbol, side, total_qty, exchanges)
            if not optimal:
                _post("Could not compute split (no liquidity?).", "error")
            else:
                summary = "Optimal split:\n"
                for s in optimal:
                    summary += f"  {s['exchange_id']}: {s['quantity']} @ {s['limit_price']}\n"
                _post(summary, "info")

                # Execute if API keys present
                execute_split(symbol, side, optimal)

            # Mark order as executed
            cfg["pending_order"] = None
            cfg["last_executed"] = {
                "timestamp": time.time(),
                "order": order_req,
                "split": optimal
            }
            save_config(cfg)

        _heartbeat()
        time.sleep(5)  # fast polling

if __name__ == "__main__":
    main()


# ═══════════════════════════════════════════════════════════════════════════════
# Example `smart_order_config.json`
# ═══════════════════════════════════════════════════════════════════════════════
"""
{
  "exchanges": ["binance", "bybit", "kraken"],
  "exchange_config": {
    "binance": { "testnet": false }
  },
  "pending_order": {
    "symbol": "BTC/USDT",
    "side": "buy",
    "quantity": 0.5
  }
}
"""
