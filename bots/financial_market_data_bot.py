#!/usr/bin/env python3
"""
financial_market_data_bot.py — Financial Market Data Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Normalizes real‑time websocket streams from Alpaca,
Binance, Interactive Brokers, and Polygon.io and posts
updates to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install websocket-client alpaca-py ib_insync requests

Configuration
─────────────
Place `market_data_config.json` in the same directory:

{
  "providers": [
    {
      "name": "alpaca",
      "enabled": true,
      "api_key": "your_api_key",
      "secret_key": "your_secret_key",
      "paper": true,
      "symbols": ["AAPL", "TSLA"]
    },
    {
      "name": "binance",
      "enabled": true,
      "symbols": ["btcusdt", "ethusdt"],
      "stream_type": "ticker"   // "ticker" or "trade"
    },
    {
      "name": "polygon",
      "enabled": true,
      "api_key": "your_polygon_key",
      "symbols": ["AAPL", "TSLA"],
      "feed": "stocks"          // "stocks" or "crypto"
    },
    {
      "name": "ib",
      "enabled": true,
      "host": "127.0.0.1",
      "port": 7497,
      "client_id": 1,
      "symbols": [{"symbol": "AAPL", "secType": "STK", "exchange": "SMART", "currency": "USD"}]
    }
  ],
  "output_format": {
    "fields": ["provider", "symbol", "price", "volume", "timestamp"]
  }
}
"""

import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "financial_market_data_bot"
BOT_NAME = "Financial Market Data"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "market_data_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
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
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
            "bot_name": BOT_NAME,
            "status":   "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── Normalizer ──────────────────────────────────────────────────
def normalize_data(provider: str, raw: dict) -> dict:
    """
    Create a uniform message for the hub from raw market data.
    """
    common = {
        "provider": provider,
        "symbol": raw.get("symbol", ""),
        "price": float(raw.get("price", 0)),
        "volume": float(raw.get("volume", 0)) if raw.get("volume") is not None else 0.0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "raw": raw  # optional
    }
    return common

# ── Provider threads ────────────────────────────────────────────

def alpaca_stream(cfg: dict) -> None:
    """Connect to Alpaca Markets websocket and stream trades/quotes."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.stream import TradingStream
    except ImportError:
        _post("Alpaca‑py not installed. pip install alpaca-py", "error")
        return

    key = cfg.get("api_key")
    secret = cfg.get("secret_key")
    paper = cfg.get("paper", True)
    symbols = cfg.get("symbols", [])
    if not symbols:
        return

    client = TradingClient(key, secret, paper=paper)
    stream = TradingStream(key, secret, paper=paper)

    async def on_trade(trade):
        data = normalize_data("alpaca", {
            "symbol": trade.symbol,
            "price": trade.price,
            "volume": trade.size,
            "timestamp": trade.timestamp.isoformat() if trade.timestamp else ""
        })
        _post(f"Alpaca trade: {data['symbol']} ${data['price']}", "info", data)

    async def on_quote(quote):
        data = normalize_data("alpaca", {
            "symbol": quote.symbol,
            "price": (quote.bid_price + quote.ask_price) / 2,
            "volume": 0,
            "bid": quote.bid_price,
            "ask": quote.ask_price
        })
        _post(f"Alpaca quote: {data['symbol']} bid/ask {quote.bid_price}/{quote.ask_price}", "info", data)

    stream.subscribe_trades(on_trade, *symbols)
    stream.subscribe_quotes(on_quote, *symbols)

    try:
        stream.run()
    except Exception as e:
        _post(f"Alpaca stream error: {e}", "error")

def binance_stream(cfg: dict) -> None:
    """Connect to Binance websocket for ticker/trade streams."""
    try:
        import websocket
    except ImportError:
        _post("websocket-client not installed", "error")
        return

    symbols = cfg.get("symbols", [])
    stream_type = cfg.get("stream_type", "ticker")
    if not symbols:
        return

    base_url = "wss://stream.binance.com:9443/ws"
    streams = []
    for sym in symbols:
        if stream_type == "trade":
            streams.append(f"{sym.lower()}@trade")
        else:
            streams.append(f"{sym.lower()}@ticker")
    url = f"{base_url}/{streams[0]}" if len(streams) == 1 else f"{base_url}/stream?streams={'/'.join(streams)}"

    def on_message(ws, message):
        data = json.loads(message)
        # If combined stream, data has "stream" and "data" fields
        if "stream" in data and "data" in data:
            event = data["data"]
            sym = data["stream"].split("@")[0].upper()
        else:
            event = data
            # Extract symbol from event
            sym = event.get("s", "").upper()
        if stream_type == "trade":
            norm = normalize_data("binance", {
                "symbol": sym,
                "price": event.get("p", 0),
                "volume": event.get("q", 0)
            })
        else:
            norm = normalize_data("binance", {
                "symbol": sym,
                "price": event.get("c", 0),   # last price
                "volume": event.get("v", 0)
            })
        _post(f"Binance {'trade' if stream_type == 'trade' else 'ticker'}: {norm['symbol']} ${norm['price']}", "info", norm)

    def on_error(ws, error):
        _post(f"Binance websocket error: {error}", "error")

    def on_close(ws, close_status_code, close_msg):
        _post(f"Binance websocket closed: {close_msg}", "warning")
        # Reconnect after delay
        time.sleep(10)
        binance_stream(cfg)

    ws = websocket.WebSocketApp(url,
                                on_message=on_message,
                                on_error=on_error,
                                on_close=on_close)
    ws.run_forever()

def polygon_stream(cfg: dict) -> None:
    """Connect to Polygon.io websocket."""
    try:
        import websocket
    except ImportError:
        _post("websocket-client not installed", "error")
        return

    api_key = cfg.get("api_key")
    symbols = cfg.get("symbols", [])
    if not api_key or not symbols:
        return

    url = "wss://socket.polygon.io/stocks"  # or crypto
    def on_open(ws):
        # Authenticate
        ws.send(json.dumps({"action": "auth", "params": api_key}))
        # Subscribe
        sub_msg = {"action": "subscribe", "params": ",".join([f"T.{s}" for s in symbols])}
        ws.send(json.dumps(sub_msg))

    def on_message(ws, message):
        data = json.loads(message)
        for msg in data:
            if msg.get("ev") == "T":  # trade
                norm = normalize_data("polygon", {
                    "symbol": msg.get("sym", ""),
                    "price": msg.get("p", 0),
                    "volume": msg.get("s", 0),
                    "timestamp": datetime.fromtimestamp(msg.get("t", 0) / 1000, tz=timezone.utc).isoformat()
                })
                _post(f"Polygon trade: {norm['symbol']} ${norm['price']}", "info", norm)

    def on_error(ws, error):
        _post(f"Polygon ws error: {error}", "error")

    ws = websocket.WebSocketApp(url,
                                on_open=on_open,
                                on_message=on_message,
                                on_error=on_error)
    ws.run_forever()

def ib_stream(cfg: dict) -> None:
    """Connect to Interactive Brokers TWS/Gateway using ib_insync."""
    try:
        from ib_insync import IB, Stock, Crypto, util
    except ImportError:
        _post("ib_insync not installed", "error")
        return

    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 7497))
    client_id = int(cfg.get("client_id", 1))
    symbols = cfg.get("symbols", [])

    ib = IB()
    ib.connect(host, port, clientId=client_id)

    for sym_cfg in symbols:
        sec_type = sym_cfg.get("secType", "STK")
        if sec_type == "STK":
            contract = Stock(sym_cfg["symbol"], sym_cfg.get("exchange", "SMART"), sym_cfg.get("currency", "USD"))
        elif sec_type == "CRYPTO":
            contract = Crypto(sym_cfg["symbol"], "PAXOS", sym_cfg.get("currency", "USD"))  # PAXOS is crypto exchange for IB
        else:
            continue
        ib.qualifyContracts(contract)

        def on_ticker(ticker):
            if ticker.lastPrice and ticker.lastSize:
                norm = normalize_data("ib", {
                    "symbol": ticker.contract.symbol,
                    "price": ticker.lastPrice,
                    "volume": ticker.lastSize
                })
                _post(f"IB trade: {norm['symbol']} ${norm['price']}", "info", norm)

        ticker = ib.reqMktData(contract, '', False, False)
        ticker.updateEvent += lambda t, contract=contract: on_ticker(t)

    ib.run()  # blocks indefinitely

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Financial Market Data Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        providers = config.get("providers", [])
        threads = []

        for prov_cfg in providers:
            name = prov_cfg.get("name")
            if not prov_cfg.get("enabled", True):
                continue
            if name == "alpaca":
                t = threading.Thread(target=alpaca_stream, args=(prov_cfg,), daemon=True)
                t.start()
                threads.append(t)
            elif name == "binance":
                t = threading.Thread(target=binance_stream, args=(prov_cfg,), daemon=True)
                t.start()
                threads.append(t)
            elif name == "polygon":
                t = threading.Thread(target=polygon_stream, args=(prov_cfg,), daemon=True)
                t.start()
                threads.append(t)
            elif name == "ib":
                t = threading.Thread(target=ib_stream, args=(prov_cfg,), daemon=True)
                t.start()
                threads.append(t)
            else:
                _post(f"Unknown provider: {name}", "warning")

        # Keep main thread alive, send heartbeats
        while True:
            _heartbeat()
            time.sleep(10)

if __name__ == "__main__":
    main()
