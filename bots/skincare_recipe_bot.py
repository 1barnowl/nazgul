#!/usr/bin/env python3
"""
skincare_recipe_bot.py — DIY Skincare Recipe AI Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Generates simple, all‑natural skincare recipes (masks,
scrubs) and monetises by recommending raw ingredients
via Amazon affiliate links. Uses an LLM (OpenAI) for
recipe generation and a local catalog of ingredients
with affiliate links. Exposes an HTTP API and optional
file watch.

Attachable to the Nazgul BotController.

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `skincare_recipe_config.json` in the same directory:

{
  "llm": {
    "provider": "openai",
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "temperature": 0.7,
    "max_tokens": 500,
    "endpoint": null
  },
  "catalog_file": "skincare_affiliate_catalog.json",
  "http_port": 9640,
  "file_watch": {
    "enabled": false,
    "directory": "/data/skincare_requests",
    "output_directory": "/data/skincare_recipes"
  },
  "heartbeat_interval": 30
}

Catalog file (`skincare_affiliate_catalog.json`) format:
[
  {
    "ingredient": "raw honey",
    "amazon_affiliate_link": "https://amazon.com/dp/B000...?tag=yourtag-20",
    "image_url": "https://...",
    "notes": "organic preferred"
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
BOT_ID   = "skincare_recipe_bot"
BOT_NAME = "DIY Skincare Recipe AI Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "skincare_recipe_config.json"
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

# ── LLM call ─────────────────────────────────────────────────────
def call_llm(prompt: str, config: dict) -> Optional[str]:
    llm_cfg = config.get("llm", {})
    api_key = llm_cfg.get("api_key")
    if not api_key:
        return None
    endpoint = llm_cfg.get("endpoint") or "https://api.openai.com/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }
    data = {
        "model": llm_cfg.get("model", "gpt-4o-mini"),
        "messages": [{"role": "user", "content": prompt}],
        "temperature": float(llm_cfg.get("temperature", 0.7)),
        "max_tokens": int(llm_cfg.get("max_tokens", 500))
    }
    try:
        resp = requests.post(endpoint, json=data, headers=headers, timeout=20)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            _post(f"LLM error {resp.status_code}: {resp.text[:200]}", "warning")
            return None
    except Exception as e:
        _post(f"LLM call failed: {e}", "warning")
        return None

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

def find_affiliate_links(ingredients: List[str], catalog: List[Dict]) -> Dict[str, Dict]:
    """Return a dict mapping ingredient name -> catalog entry if found."""
    link_map = {}
    for ing in ingredients:
        ing_lower = ing.strip().lower()
        for entry in catalog:
            if entry.get("ingredient", "").strip().lower() == ing_lower:
                link_map[ing] = {
                    "affiliate_link": entry.get("amazon_affiliate_link", ""),
                    "image_url": entry.get("image_url", ""),
                    "notes": entry.get("notes", "")
                }
                break
    return link_map

# ── Recipe generation ────────────────────────────────────────────
def generate_recipe(skin_type: str, concern: str, llm_config: dict, catalog: List[Dict]) -> dict:
    prompt = f"""Create a simple all-natural DIY skincare recipe for a person with {skin_type} skin and concern about {concern}.
The recipe should use common natural ingredients (like honey, oatmeal, yogurt, oils, etc.).
Return a JSON object with the following structure:
{{
  "name": "Recipe Name",
  "ingredients": ["ingredient 1", "ingredient 2", ...],
  "instructions": "Step-by-step instructions"
}}
Only output the JSON, no other text."""
    response = call_llm(prompt, llm_config)
    if not response:
        return {"error": "LLM did not respond"}
    try:
        # Attempt to parse JSON from response (may include markdown fences)
        cleaned = response.strip()
        if cleaned.startswith("```"):
            lines = cleaned.splitlines()
            lines = [l for l in lines if not l.startswith("```")]
            cleaned = "\n".join(lines)
        recipe = json.loads(cleaned)
    except Exception as e:
        _post(f"Failed to parse LLM recipe: {e}", "warning")
        # Fallback: return raw text with a generic structure
        recipe = {
            "name": "Custom Recipe",
            "ingredients": [],
            "instructions": response
        }
    # Enrich with affiliate links
    ingredients = recipe.get("ingredients", [])
    affiliate_map = find_affiliate_links(ingredients, catalog)
    recipe["affiliate_links"] = [
        {
            "ingredient": ing,
            "link": affiliate_map[ing]["affiliate_link"] if ing in affiliate_map else None,
            "image_url": affiliate_map[ing]["image_url"] if ing in affiliate_map else None
        }
        for ing in ingredients
    ]
    return recipe

# ── HTTP API handler ─────────────────────────────────────────────
class RecipeHandler(BaseHTTPRequestHandler):
    config: Dict = {}
    catalog: List[Dict] = []

    def do_POST(self):
        if self.path == "/generate":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                skin_type = data.get("skin_type", "normal")
                concern = data.get("concern", "hydration")
                recipe = generate_recipe(skin_type, concern, self.config, self.catalog)
                summary = f"Generated recipe '{recipe.get('name')}' for {skin_type} skin, {concern}"
                _post(summary, "info", recipe)
                self._respond(200, recipe)
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

def start_http(port: int, config: Dict, catalog: List[Dict]):
    RecipeHandler.config = config
    RecipeHandler.catalog = catalog
    server = HTTPServer(("0.0.0.0", port), RecipeHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Skincare Recipe API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set, config: Dict, catalog: List[Dict]):
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
            skin_type = data.get("skin_type", "normal")
            concern = data.get("concern", "hydration")
            recipe = generate_recipe(skin_type, concern, config, catalog)
            out_name = Path(fname).stem + "_recipe.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w") as out:
                json.dump(recipe, out, indent=2)
            _post(f"Generated recipe for {fname} → {out_name}", "info")
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("DIY Skincare Recipe AI Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        catalog_file = config.get("catalog_file", "skincare_affiliate_catalog.json")
        catalog = load_catalog(catalog_file)
        port = int(config.get("http_port", 9640))
        start_http(port, config, catalog)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache, config, catalog)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
