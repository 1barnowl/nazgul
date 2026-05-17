#!/usr/bin/env python3
"""
tanning_shade_matcher_bot.py — Tanning Product Shade Matcher Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Recommends self‑tan products based on user’s skin tone,
with affiliate links to purchase.  Attachable to the
Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `tanning_shade_config.json` in the same directory:

{
  "catalog_file": "tanning_products.json",
  "http_port": 9690,
  "file_watch": {
    "enabled": false,
    "directory": "/data/tanning_requests",
    "output_directory": "/data/tanning_recommendations"
  },
  "heartbeat_interval": 30
}

Catalog file (`tanning_products.json`) – array of objects:
[
  {
    "id": "bali_body_watermelon",
    "name": "Bali Body Watermelon Tanning Mousse",
    "shade_categories": ["fair", "medium"],
    "type": "mousse",
    "price": 29.95,
    "affiliate_link": "https://example.com/bali-mousse?ref=you",
    "description": "Lightweight, fast-drying mousse that gives a natural golden tan.",
    "image_url": "https://..."
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
from typing import List, Dict, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "tanning_shade_matcher_bot"
BOT_NAME = "Tanning Product Shade Matcher"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "tanning_shade_config.json"
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

# ── Catalog loading ─────────────────────────────────────────────
def load_catalog(path: str) -> List[Dict]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

# ── Matching logic ──────────────────────────────────────────────
# We define a mapping from common skin tone descriptions to the categories used in our catalog.
# The user can provide a free‑text skin tone, but we'll normalise it.
SKIN_TONE_ALIASES = {
    "fair": ["fair", "light", "pale", "porcelain"],
    "medium": ["medium", "olive", "tan", "beige", "caramel"],
    "dark": ["dark", "deep", "ebony", "rich", "dark brown"],
}

def normalise_skin_tone(user_input: str) -> Optional[str]:
    """Return the canonical shade category (fair, medium, dark) from user input."""
    text = user_input.strip().lower()
    for category, aliases in SKIN_TONE_ALIASES.items():
        if any(alias in text for alias in aliases):
            return category
    # Fallback: if user input is exactly one word matching a category
    if text in SKIN_TONE_ALIASES:
        return text
    return None  # cannot normalise

def recommend_products(skin_tone: str, product_type: Optional[str] = None,
                       catalog: List[Dict]) -> List[Dict]:
    """Return matching products sorted by relevance (maybe by rating/price)."""
    canonical = normalise_skin_tone(skin_tone)
    if not canonical:
        # If we cannot normalise, show all products? better to return nothing.
        return []

    # Filter by shade category
    matches = []
    for product in catalog:
        shade_cats = product.get("shade_categories", [])
        if canonical in shade_cats:
            if product_type and product.get("type") != product_type:
                continue
            matches.append(product)

    # Sort by price ascending (or rating if available)
    matches.sort(key=lambda x: x.get("price", 0))
    return matches

# ── HTTP API handler ─────────────────────────────────────────────
class ShadeMatcherHandler(BaseHTTPRequestHandler):
    catalog: List[Dict] = []

    def do_POST(self):
        if self.path == "/recommend":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                skin_tone = data.get("skin_tone", "")
                product_type = data.get("product_type")   # optional: "mousse", "lotion", "spray"
                if not skin_tone:
                    self._respond(400, {"error": "Missing 'skin_tone' field"})
                    return
                results = recommend_products(skin_tone, product_type, self.catalog)
                summary = f"Recommended {len(results)} products for '{skin_tone}'"
                _post(summary, "info", {"skin_tone": skin_tone, "count": len(results)})
                self._respond(200, {"recommendations": results})
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

def start_http(port: int, catalog: List[Dict]):
    ShadeMatcherHandler.catalog = catalog
    server = HTTPServer(("0.0.0.0", port), ShadeMatcherHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Tanning Shade Matcher API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set, catalog: List[Dict]):
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
            skin_tone = data.get("skin_tone", "")
            product_type = data.get("product_type")
            results = recommend_products(skin_tone, product_type, catalog)
            out_name = Path(fname).stem + "_matched.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w") as out:
                json.dump({"query": data, "recommendations": results}, out, indent=2)
            _post(f"Processed {fname} → {out_name}", "info")
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Tanning Product Shade Matcher Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        catalog_file = config.get("catalog_file", "tanning_products.json")
        catalog = load_catalog(catalog_file)
        if not catalog:
            _post("Product catalog is empty or missing", "warning")
        else:
            _post(f"Loaded {len(catalog)} tanning products", "info")

        port = int(config.get("http_port", 9690))
        start_http(port, catalog)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache, catalog)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
