#!/usr/bin/env python3
"""
entity_extraction_bot.py — Entity Extraction Bot (NER)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Identifies people, companies, emails, phones, and addresses
in free text using spaCy or GLiNER, exposing a simple HTTP API
for other bots to use. Results are reported to the Nazgul
BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install spacy gliner requests
    python -m spacy download en_core_web_sm

Configuration
─────────────
Place `entity_extraction_config.json` in the same directory:

{
  "engine": "spacy",             // "spacy" or "gliner" or "both"
  "spacy_model": "en_core_web_sm",
  "gliner_model": "urchade/gliner_medium-v2.1",
  "gliner_labels": ["person", "organization", "date", "email", "phone number"],
  "http_port": 9555,
  "file_watch": {                // optional
    "enabled": false,
    "directory": "/data/incoming",
    "output_dir": "/data/entities"
  },
  "heartbeat_interval": 30
}
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import List, Dict, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "entity_extraction_bot"
BOT_NAME = "Entity Extractor"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "entity_extraction_config.json"
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

# ── NER engines ──────────────────────────────────────────────────

class SpaCyNER:
    def __init__(self, model_name: str):
        import spacy
        try:
            self.nlp = spacy.load(model_name)
        except OSError:
            _post(f"Downloading spaCy model {model_name}...")
            spacy.cli.download(model_name)
            self.nlp = spacy.load(model_name)

    def extract(self, text: str) -> List[Dict]:
        doc = self.nlp(text)
        entities = []
        for ent in doc.ents:
            entities.append({
                "text": ent.text,
                "label": ent.label_,
                "start": ent.start_char,
                "end": ent.end_char
            })
        # Also use regex for emails/phones not captured by spaCy
        extras = self._regex_extras(text)
        return entities + extras

    @staticmethod
    def _regex_extras(text: str) -> List[Dict]:
        extras = []
        for match in re.finditer(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text):
            extras.append({
                "text": match.group(),
                "label": "EMAIL",
                "start": match.start(),
                "end": match.end()
            })
        for match in re.finditer(r'\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b', text):
            extras.append({
                "text": match.group(),
                "label": "PHONE",
                "start": match.start(),
                "end": match.end()
            })
        return extras

class GLiNER_NER:
    def __init__(self, model_name: str, labels: List[str]):
        from gliner import GLiNER
        self.model = GLiNER.from_pretrained(model_name)
        self.labels = labels

    def extract(self, text: str) -> List[Dict]:
        entities = self.model.predict_entities(text, self.labels)
        return [
            {
                "text": ent["text"],
                "label": ent["label"],
                "start": ent["start"],
                "end": ent["end"]
            }
            for ent in entities
        ]

# ── Global NER engine (initialized once) ─────────────────────────
ner_engine = None

def init_engine(config: dict):
    global ner_engine
    engine = config.get("engine", "spacy")
    if engine == "spacy":
        model = config.get("spacy_model", "en_core_web_sm")
        ner_engine = SpaCyNER(model)
    elif engine == "gliner":
        model = config.get("gliner_model", "urchade/gliner_medium-v2.1")
        labels = config.get("gliner_labels", ["person", "organization", "date"])
        ner_engine = GLiNER_NER(model, labels)
    elif engine == "both":
        # Create a composite engine (simple list concatenation)
        class BothNER:
            def __init__(self, spacy_model, gliner_model, gliner_labels):
                self.spacy = SpaCyNER(spacy_model)
                self.gliner = GLiNER_NER(gliner_model, gliner_labels)

            def extract(self, text: str) -> List[Dict]:
                return self.spacy.extract(text) + self.gliner.extract(text)

        spacy_model = config.get("spacy_model", "en_core_web_sm")
        gliner_model = config.get("gliner_model", "urchade/gliner_medium-v2.1")
        gliner_labels = config.get("gliner_labels", ["person", "organization", "date"])
        ner_engine = BothNER(spacy_model, gliner_model, gliner_labels)
    else:
        _post(f"Unknown engine '{engine}', falling back to spaCy", "warning")
        ner_engine = SpaCyNER("en_core_web_sm")

# ── HTTP API ─────────────────────────────────────────────────────
class NERHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/extract":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                text = data.get("text", "")
                if not text:
                    self._respond(400, {"error": "Missing 'text' field"})
                    return
                entities = ner_engine.extract(text)
                summary = f"Extracted {len(entities)} entities from text ({len(text)} chars)"
                _post(summary, "info", {"entity_count": len(entities), "text_length": len(text)})
                self._respond(200, {"entities": entities})
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

def start_http(port: int):
    server = HTTPServer(("0.0.0.0", port), NERHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Entity Extraction API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set):
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for fname in entries:
        if not fname.endswith(".txt"):
            continue
        fpath = os.path.join(directory, fname)
        if fpath in processed:
            continue
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                text = f.read()
            entities = ner_engine.extract(text)
            out_name = Path(fname).stem + ".json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as out:
                json.dump({"source": fname, "entities": entities}, out, indent=2)
            _post(f"Processed {fname} -> {out_name}", "info", {"file": fname})
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Entity Extraction Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        init_engine(config)

        port = int(config.get("http_port", 9555))
        start_http(port)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_dir")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
