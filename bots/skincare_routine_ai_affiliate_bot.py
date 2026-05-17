#!/usr/bin/env python3
"""
skincare_routine_ai_affiliate_bot.py — Skincare Routine AI Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Users describe their skin concerns (or optionally provide a selfie URL)
and the bot builds a full skincare routine with direct‑to‑retailer
affiliate links.  Uses OpenAI for routine generation and a local product
catalog for matching.  Attachable to the Nazgul BotController.

Requirements
────────────
    pip install requests openai

Configuration
─────────────
Place `skincare_routine_ai_config.json` in the same directory:

{
  "llm": {
    "api_key": "sk-...",
    "model": "gpt-4o-mini",          // or "gpt-4o" if you have access
    "temperature": 0.7,
    "max_tokens": 600
  },
  "catalog_file": "skincare_products.json",
  "http_port": 9685,
  "file_watch": {
    "enabled": false,
    "directory": "/data/skincare_requests",
    "output_directory": "/data/skincare_routines"
  },
  "heartbeat_interval": 30
}

Catalog file (`skincare_products.json`) – array of objects:
[
  {
    "name": "CeraVe Hydrating Facial Cleanser",
    "category": "cleanser",
    "affiliate_link": "https://amazon.com/dp/B01MSSDEPK?tag=youraffiliate-20",
    "description": "Gentle hydrating cleanser with ceramides"
  },
  ...
]
"""

import base64
import json
import os
import time
import threading
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
import openai

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "skincare_routine_ai_affiliate_bot"
BOT_NAME = "Skincare Routine AI Affiliate"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "skincare_routine_ai_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(
            f"{HUB}/ingest",
            json={
                "bot_id": BOT_ID,
                "bot_name": BOT_NAME,
                "summary": summary,
                "level": level,
                "payload": payload or {},
            },
            timeout=5,
        )
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(
            f"{HUB}/heartbeat/{BOT_ID}",
            json={"bot_name": BOT_NAME, "status": "online"},
            timeout=3,
        )
    except Exception:
        pass
    _last_hb = time.time()

# ── Catalog ─────────────────────────────────────────────────────
def load_catalog(path: str) -> List[Dict]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        _post(f"Failed to load catalog {path}", "error")
        return []

# ── LLM routine generation ──────────────────────────────────────
def generate_routine(llm_cfg: dict, skin_concern: str, image_url: Optional[str] = None) -> Optional[List[Dict]]:
    """
    Ask LLM to propose a 3‑step routine (cleanser, serum, moisturiser)
    based on the user's description and optionally an image.
    Returns a list of dicts: [{"step": "cleanser", "reason": "..."}, ...]
    or None on failure.
    """
    if not llm_cfg.get("api_key"):
        _post("OpenAI API key not set", "error")
        return None

    openai.api_key = llm_cfg["api_key"]
    model = llm_cfg.get("model", "gpt-4o-mini")

    messages = [
        {
            "role": "system",
            "content": (
                "You are a professional dermatologist assistant. "
                "Based on the user's skin concerns, recommend a simple 3‑step skincare routine: "
                "1) cleanser, 2) serum/treatment, 3) moisturiser. "
                "For each step, briefly explain why it suits their skin type. "
                "Return the answer as a JSON array of objects with keys 'step' and 'reason'. "
                "Do not include any other text."
            )
        },
        {
            "role": "user",
            "content": f"Skin concerns: {skin_concern}"
        }
    ]

    # If an image URL is provided, add it as a vision message
    if image_url:
        # For gpt-4o-mini, we can use image_url content part
        # For gpt-4o, we need to use the vision model (gpt-4o or gpt-4o-mini). All support this.
        # Replace the user message with multimodal content
        messages[1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Skin concerns: {skin_concern}"},
                {
                    "type": "image_url",
                    "image_url": {"url": image_url, "detail": "low"}
                }
            ]
        }

    try:
        response = openai.ChatCompletion.create(
            model=model,
            messages=messages,
            temperature=llm_cfg.get("temperature", 0.7),
            max_tokens=llm_cfg.get("max_tokens", 600)
        )
        raw = response.choices[0].message.content.strip()
        # Clean up possible markdown fences
        if raw.startswith("```"):
            lines = raw.splitlines()
            lines = [l for l in lines if not l.startswith("```")]
            raw = "\n".join(lines)
        routine = json.loads(raw)
        if isinstance(routine, list):
            return routine
        else:
            return None
    except Exception as e:
        _post(f"LLM routine generation failed: {e}", "error")
        return None

def attach_affiliate_links(routine: List[Dict], catalog: List[Dict]) -> List[Dict]:
    """
    For each routine step, find a matching product in the catalog (by category)
    and add affiliate link info.
    """
    enriched = []
    for step in routine:
        category = step.get("step", "").lower()
        # Map possible LLM output to our categories
        cat_map = {
            "cleanser": "cleanser",
            "serum": "serum",
            "treatment": "serum",
            "moisturiser": "moisturizer",
            "moisturizer": "moisturizer"
        }
        mapped_cat = cat_map.get(category, category)
        # Find first product that matches category
        product = next((p for p in catalog if p.get("category", "").lower() == mapped_cat), None)
        if product:
            step["product_name"] = product["name"]
            step["affiliate_link"] = product.get("affiliate_link", "")
            step["product_description"] = product.get("description", "")
        else:
            # fallback generic recommendation
            step["product_name"] = f"Any {category}"
            step["affiliate_link"] = ""
            step["product_description"] = ""
        enriched.append(step)
    return enriched

# ── HTTP API handler ─────────────────────────────────────────────
class SkincareHandler(BaseHTTPRequestHandler):
    catalog: List[Dict] = []
    llm_config: Dict = {}

    def do_POST(self):
        if self.path == "/routine":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                concern = data.get("concern", "")
                image_url = data.get("image_url")  # optional
                if not concern:
                    self._respond(400, {"error": "Missing 'concern' field"})
                    return
                routine = generate_routine(self.llm_config, concern, image_url)
                if not routine:
                    self._respond(500, {"error": "Failed to generate routine"})
                    return
                enriched = attach_affiliate_links(routine, self.catalog)
                summary = f"Generated routine for '{concern[:50]}...' with {len(enriched)} steps"
                _post(summary, "info", {"concern": concern, "routine": enriched})
                self._respond(200, {"routine": enriched})
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

def start_http(port: int, catalog: List[Dict], llm_config: Dict):
    SkincareHandler.catalog = catalog
    SkincareHandler.llm_config = llm_config
    server = HTTPServer(("0.0.0.0", port), SkincareHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Skincare Routine API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set,
                    catalog: List[Dict], llm_config: Dict):
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
            concern = data.get("concern", "")
            image_url = data.get("image_url")
            routine = generate_routine(llm_config, concern, image_url)
            if routine:
                enriched = attach_affiliate_links(routine, catalog)
                out_name = Path(fname).stem + "_routine.json"
                out_path = os.path.join(output_dir, out_name)
                with open(out_path, "w") as out:
                    json.dump({"concern": concern, "routine": enriched}, out, indent=2)
                _post(f"Processed {fname} → {out_name}", "info")
            else:
                _post(f"Failed to process {fname}", "warning")
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Skincare Routine AI Affiliate Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        llm_cfg = config.get("llm", {})
        catalog_file = config.get("catalog_file", "skincare_products.json")
        catalog = load_catalog(catalog_file)
        if not catalog:
            _post("Product catalog empty/missing", "warning")
        else:
            _post(f"Loaded {len(catalog)} products", "info")

        port = int(config.get("http_port", 9685))
        start_http(port, catalog, llm_cfg)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache, catalog, llm_cfg)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
