#!/usr/bin/env python3
"""
upsell_advisor_bot.py — Upsell / Cross‑Sell Advisor Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Suggests complementary products (cross‑sell) or upgrades
(upsell) during the sales conversation to increase basket
size. Uses a curated product association catalog and returns
recommendations with affiliate links.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `upsell_config.json` in the same directory:

{
  "catalog_file": "product_associations.json",
  "http_port": 9680,
  "file_watch": {
    "enabled": false,
    "directory": "/data/upsell_requests",
    "output_directory": "/data/upsell_recommendations"
  },
  "heartbeat_interval": 30
}

The catalog file (`product_associations.json`) should be an array of objects:

[
  {
    "id": "prod_basic_shirt",
    "name": "Classic Cotton Tee",
    "price": 19.99,
    "category": "shirts",
    "affiliate_link": "https://shop.example.com/tee?ref=you",
    "upsell_id": "prod_premium_shirt",
    "cross_sell_ids": ["prod_jeans", "prod_sneakers"]
  },
  {
    "id": "prod_premium_shirt",
    "name": "Luxury Egyptian Cotton Shirt",
    "price": 49.99,
    "category": "shirts",
    "affiliate_link": "https://shop.example.com/luxury-tee?ref=you",
    "upsell_id": null,
    "cross_sell_ids": ["prod_jeans", "prod_belt"]
  },
  ...
]
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import List, Dict, Optional, Set

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "upsell_advisor_bot"
BOT_NAME = "Upsell / Cross‑Sell Advisor"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "upsell_config.json"
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

# ── Catalog loading ──────────────────────────────────────────────
def load_catalog(path: str) -> List[Dict]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

# ── Recommendation engine ───────────────────────────────────────
class UpsellEngine:
    def __init__(self, catalog: List[Dict]):
        self.catalog = catalog
        self.products_by_id = {p["id"]: p for p in catalog if "id" in p}

    def recommend(self, product_ids: List[str]) -> Dict:
        """
        Given a list of product IDs (current cart / viewed items),
        return upsell and cross-sell suggestions.
        """
        seen_ids = set(product_ids)
        upsells = []
        cross_sells = []

        for pid in product_ids:
            product = self.products_by_id.get(pid)
            if not product:
                continue

            # Upsell (higher tier)
            upsell_id = product.get("upsell_id")
            if upsell_id and upsell_id not in seen_ids:
                upsell_product = self.products_by_id.get(upsell_id)
                if upsell_product:
                    upsells.append(upsell_product)
                    seen_ids.add(upsell_id)

            # Cross‑sells (complementary)
            cross_ids = product.get("cross_sell_ids", [])
            for cid in cross_ids:
                if cid not in seen_ids:
                    cross_product = self.products_by_id.get(cid)
                    if cross_product:
                        cross_sells.append(cross_product)
                        seen_ids.add(cid)

        # Remove duplicates while preserving order
        unique_upsells = list({p["id"]: p for p in upsells}.values())
        unique_cross = list({p["id"]: p for p in cross_sells}.values())

        return {
            "upsell_suggestions": unique_upsells,
            "cross_sell_suggestions": unique_cross
        }

# ── HTTP API handler ─────────────────────────────────────────────
class AdvisorHandler(BaseHTTPRequestHandler):
    engine: UpsellEngine = None

    def do_POST(self):
        if self.path == "/recommend":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                product_ids = data.get("product_ids", [])
                if not isinstance(product_ids, list):
                    self._respond(400, {"error": "Missing 'product_ids' list"})
                    return
                result = self.engine.recommend(product_ids)
                summary = f"Upsells: {len(result['upsell_suggestions'])}, Cross‑sells: {len(result['cross_sell_suggestions'])}"
                _post(summary, "info", {"product_ids": product_ids, "result": result})
                self._respond(200, result)
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "Not found"})

    def _respond(self, code, data):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def log_message(self, *args):
        pass

def start_http(port: int, engine: UpsellEngine):
    AdvisorHandler.engine = engine
    server = HTTPServer(("0.0.0.0", port), AdvisorHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Upsell/Cross‑Sell API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set, engine: UpsellEngine):
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(directory, fname)
        if fpath in processed:
            continue
        try:
            with open(fpath, "r") as f:
                data = json.load(f)
            product_ids = data.get("product_ids", [])
            if not product_ids:
                _post(f"File {fname} missing 'product_ids'", "warning")
                processed.add(fpath)
                continue
            result = engine.recommend(product_ids)
            out_name = Path(fname).stem + "_recommendations.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w") as out:
                json.dump({"query": product_ids, "result": result}, out, indent=2)
            _post(f"Processed {fname} → {out_name}", "info")
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Upsell / Cross‑Sell Advisor Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        catalog_file = config.get("catalog_file", "product_associations.json")
        catalog = load_catalog(catalog_file)
        if not catalog:
            _post("Product association catalog is empty or missing", "warning")
        else:
            _post(f"Loaded {len(catalog)} products", "info")

        engine = UpsellEngine(catalog)
        port = int(config.get("http_port", 9680))
        start_http(port, engine)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache, engine)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
