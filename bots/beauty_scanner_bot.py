#!/usr/bin/env python3
"""
beauty_scanner_bot.py — Beauty Ingredient Scanner Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans a product barcode (UPC/EAN) via Open Food Facts, flags
harmful ingredients, and recommends cleaner alternatives with
affiliate purchase links. Exposes an HTTP API and optional
file watch.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `beauty_scanner_config.json` in the same directory:

{
  "openfoodfacts": {
    "base_url": "https://world.openfoodfacts.org/api/v2/product/{barcode}.json",
    "user_agent": "BeautyScannerBot/1.0"
  },
  "harmful_ingredients": [
    "paraben",
    "sulfate",
    "phthalate",
    "formaldehyde",
    "triclosan",
    "oxybenzone",
    "synthetic fragrance",
    "mineral oil"
  ],
  "alternatives_catalog": "clean_beauty_alternatives.json",
  "http_port": 9630,
  "file_watch": {
    "enabled": false,
    "directory": "/data/barcode_requests",
    "output_directory": "/data/scan_results"
  },
  "heartbeat_interval": 30
}

The alternatives catalog file (`clean_beauty_alternatives.json`) should be an array:
[
  {
    "product_name": "Gentle Rose Moisturizer",
    "brand": "CleanSkin",
    "category": "moisturizer",
    "affiliate_link": "https://example.com/clean-moisturizer?ref=you"
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
BOT_ID   = "beauty_scanner_bot"
BOT_NAME = "Beauty Ingredient Scanner"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "beauty_scanner_config.json"
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

# ── Product lookup via Open Food Facts ───────────────────────────
def fetch_product(barcode: str, config: dict) -> Dict:
    of_cfg = config.get("openfoodfacts", {})
    url = of_cfg.get("base_url", "https://world.openfoodfacts.org/api/v2/product/{barcode}.json")
    headers = {"User-Agent": of_cfg.get("user_agent", "BeautyScannerBot/1.0")}
    url = url.format(barcode=barcode)
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        if resp.status_code != 200:
            return {"error": f"Open Food Facts API returned {resp.status_code}"}
        data = resp.json()
        if data.get("status") != 1:
            return {"error": "Product not found"}
        product = data.get("product", {})
        return {"success": True, "product": product}
    except Exception as e:
        return {"error": str(e)}

def extract_ingredients(product: Dict) -> List[str]:
    ingredients = product.get("ingredients", [])
    if ingredients:
        # ingredients is a list of dict with "text" or "id"
        return [i.get("text", "").lower() for i in ingredients if i.get("text")]
    # fallback to ingredients_text field
    text = product.get("ingredients_text", "")
    if text:
        # split by comma, but careful with sub-ingredients
        return [ing.strip().lower() for ing in text.split(",") if ing.strip()]
    return []

def find_harmful_ingredients(ingredients: List[str], harmful_list: List[str]) -> List[str]:
    """Return ingredients that contain any harmful substring."""
    found = []
    for ing in ingredients:
        for bad in harmful_list:
            if bad.lower() in ing:
                found.append(ing)
                break
    return found

def load_alternatives(catalog_path: str) -> List[Dict]:
    try:
        with open(catalog_path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

def recommend_alternatives(category: Optional[str], alternatives: List[Dict]) -> List[Dict]:
    if not category:
        return alternatives[:3]  # return top 3 if no category
    matches = [a for a in alternatives if a.get("category", "").lower() == category.lower()]
    if not matches:
        # try partial match
        matches = [a for a in alternatives if category.lower() in a.get("category", "").lower()]
    return matches[:3] if matches else alternatives[:3]

# ── HTTP API handler ─────────────────────────────────────────────
class ScannerHandler(BaseHTTPRequestHandler):
    config: Dict = {}
    alternatives: List[Dict] = []
    harmful: List[str] = []

    def do_POST(self):
        if self.path == "/scan":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                barcode = data.get("barcode", "")
                if not barcode:
                    self._respond(400, {"error": "Missing 'barcode'"})
                    return
                result = fetch_product(barcode, self.config)
                if "error" in result:
                    self._respond(404, result)
                    return
                product = result["product"]
                product_name = product.get("product_name", "Unknown product")
                categories = product.get("categories_tags", []) or product.get("categories", [])
                # pick first category as main category
                main_category = None
                if categories:
                    # categories_tags are like ["en:moisturizers"]
                    main_category = categories[0].split(":")[-1] if ":" in categories[0] else categories[0]

                ingredients = extract_ingredients(product)
                bad_ingredients = find_harmful_ingredients(ingredients, self.harmful)

                summary = f"Scanned {product_name} (barcode {barcode}): {len(bad_ingredients)} harmful ingredients"
                _post(summary, "info", {"barcode": barcode, "product_name": product_name, "bad_ingredients": bad_ingredients})

                recommendations = []
                if bad_ingredients:
                    recommendations = recommend_alternatives(main_category, self.alternatives)

                self._respond(200, {
                    "barcode": barcode,
                    "product_name": product_name,
                    "ingredients_found": ingredients,
                    "harmful_ingredients": bad_ingredients,
                    "recommended_alternatives": recommendations
                })
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

def start_http(port: int, config: Dict, alternatives: List[Dict], harmful: List[str]):
    ScannerHandler.config = config
    ScannerHandler.alternatives = alternatives
    ScannerHandler.harmful = harmful
    server = HTTPServer(("0.0.0.0", port), ScannerHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Beauty Scanner API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set, config: Dict, alternatives: List[Dict], harmful: List[str]):
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
            barcode = data.get("barcode")
            if not barcode:
                _post(f"File {fname} missing 'barcode'", "warning")
                processed.add(fpath)
                continue
            result = fetch_product(barcode, config)
            if "error" in result:
                _post(f"Barcode lookup failed for {fname}: {result['error']}", "error")
                continue
            product = result["product"]
            product_name = product.get("product_name", "Unknown")
            categories = product.get("categories_tags", []) or product.get("categories", [])
            main_category = None
            if categories:
                main_category = categories[0].split(":")[-1] if ":" in categories[0] else categories[0]
            ingredients = extract_ingredients(product)
            bad = find_harmful_ingredients(ingredients, harmful)
            recommendations = recommend_alternatives(main_category, alternatives) if bad else []
            output_data = {
                "barcode": barcode,
                "product_name": product_name,
                "harmful_ingredients": bad,
                "recommendations": recommendations
            }
            out_name = Path(fname).stem + "_result.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w") as out:
                json.dump(output_data, out, indent=2)
            _post(f"Processed {fname} → {out_name}", "info")
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Beauty Ingredient Scanner Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        harmful = config.get("harmful_ingredients", [])
        alternatives_file = config.get("alternatives_catalog", "clean_beauty_alternatives.json")
        alternatives = load_alternatives(alternatives_file)

        port = int(config.get("http_port", 9630))
        start_http(port, config, alternatives, harmful)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache, config, alternatives, harmful)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
