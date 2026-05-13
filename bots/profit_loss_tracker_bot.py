#!/usr/bin/env python3
"""
profit_loss_tracker_bot.py — Profit & Loss Tracker Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Aggregates P&L from trading bots and affiliate networks
into a unified view for the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `pnl_config.json` in the same directory as this script:

{
  "sources": [
    {
      "name": "momentum_bot",
      "type": "file",
      "path": "/path/to/momentum_pnl.json",
      "currency": "USD"
    },
    {
      "name": "affiliate_network_abc",
      "type": "http",
      "url": "https://api.affiliate.com/v1/report?date=today",
      "headers": {
        "Authorization": "Bearer YOUR_TOKEN"
      },
      "json_path": "data.earnings",
      "currency": "USD"
    }
  ],
  "aggregation": {
    "alert_threshold": -500,
    "currencies": ["USD", "EUR"],
    "output_currency": "USD"
  },
  "poll_interval": 3600
}

The data returned (file or HTTP) must be a JSON array of objects with at least:
  {
    "timestamp": "2025-01-15T14:30:00Z",
    "amount": 123.45,
    "currency": "USD",
    "type": "profit"   // or "loss" if amount negative, otherwise inferred
  }
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Hub connection ─────────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "profit_loss_tracker_bot"
BOT_NAME = "Profit & Loss Tracker"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "pnl_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

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

# ── Data fetching ──────────────────────────────────────────────────────────────
def fetch_source(src: dict) -> list[dict] | None:
    """Return list of P&L records or None on failure."""
    name = src.get("name", "unknown")
    stype = src.get("type", "file")
    try:
        if stype == "file":
            path = src.get("path")
            if not path or not os.path.exists(path):
                return None
            with open(path, "r") as f:
                data = json.load(f)
            # If JSON is an object with a key like "records", allow specifying json_path
            json_path = src.get("json_path")
            if json_path:
                for part in json_path.split("."):
                    data = data.get(part, []) if isinstance(data, dict) else []
            if isinstance(data, list):
                return data
            return []

        elif stype == "http":
            url = src.get("url")
            headers = src.get("headers", {})
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            json_path = src.get("json_path")
            if json_path:
                for part in json_path.split("."):
                    if isinstance(data, dict):
                        data = data.get(part, [])
                    else:
                        data = []
            if isinstance(data, list):
                return data
            return []
        else:
            return None
    except Exception as e:
        _post(f"Failed to fetch {name}: {e}", "warning")
        return None

# ── Aggregation ────────────────────────────────────────────────────────────────
def aggregate(records: list[dict], config: dict) -> dict:
    """Sum profits and losses per source and overall, convert to output currency."""
    source_aggregates = {}
    overall_profit = 0.0
    overall_loss = 0.0
    counts = {}
    output_currency = config.get("output_currency", "USD")
    # Simple currency conversion – user must provide if needed, else assume 1:1
    fx_rates = config.get("fx_rates", {})  # e.g. {"EUR": 1.08, "JPY": 0.0067}

    for rec in records:
        try:
            src = rec.get("source", "unknown")
            amt = float(rec.get("amount", 0))
            curr = rec.get("currency", "USD")
            # Convert to output currency
            if curr != output_currency:
                rate = fx_rates.get(curr, 1.0)
                amt *= rate
            # Determine if it's profit or loss based on type field or sign
            pnl_type = rec.get("type", "")
            if pnl_type == "loss" or (aml < 0 and pnl_type != "profit"):
                loss = abs(amt)
                overall_loss += loss
                if src not in source_aggregates:
                    source_aggregates[src] = {"profit": 0.0, "loss": 0.0, "count": 0}
                source_aggregates[src]["loss"] += loss
            else:
                overall_profit += amt
                if src not in source_aggregates:
                    source_aggregates[src] = {"profit": 0.0, "loss": 0.0, "count": 0}
                source_aggregates[src]["profit"] += amt
            source_aggregates[src]["count"] += 1
        except (ValueError, TypeError):
            continue

    net = overall_profit - overall_loss
    return {
        "net": round(net, 2),
        "total_profit": round(overall_profit, 2),
        "total_loss": round(overall_loss, 2),
        "by_source": {k: {"profit": round(v["profit"],2), "loss": round(v["loss"],2), "net": round(v["profit"]-v["loss"],2), "transactions": v["count"]}
                      for k, v in source_aggregates.items()},
        "output_currency": output_currency
    }

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    _post("Profit & Loss Tracker Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        sources = config.get("sources", [])
        all_records = []
        for src in sources:
            if not src.get("enabled", True):
                continue
            data = fetch_source(src)
            if data is not None:
                # Tag records with source name for aggregation
                source_name = src.get("name", "unknown")
                for record in data:
                    record["source"] = source_name
                all_records.extend(data)

        if not all_records:
            _post("No P&L data retrieved from any source", "warning")
        else:
            agg = aggregate(all_records, config.get("aggregation", {}))
            threshold = config.get("aggregation", {}).get("alert_threshold", -500)
            net = agg["net"]
            if net < threshold:
                _post(f"P&L alert: net {net:.2f} {agg['output_currency']} below threshold {threshold}",
                      "error", agg)
            else:
                _post(f"P&L summary: net {net:.2f} {agg['output_currency']} "
                      f"(profit {agg['total_profit']:.2f} loss {agg['total_loss']:.2f})",
                      "info", agg)

        _heartbeat()
        time.sleep(int(config.get("poll_interval", 3600)))

if __name__ == "__main__":
    main()
