#!/usr/bin/env python3
"""
language_translation_bot.py — Language Translation Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Normalizes multilingual content to English (or any target
language) using Google Translate or an offline MBart model.
Exposes an HTTP API for other bots.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install deep-translator transformers sentencepiece langdetect requests

Configuration
─────────────
Place `translation_config.json` in the same directory:

{
  "engine": "google",            // "google" or "mbart"
  "target_language": "en",
  "http_port": 9570,
  "file_watch": {
    "enabled": false,
    "directory": "/data/raw_text",
    "output_directory": "/data/translated"
  },
  "heartbeat_interval": 30
}
"""

import json
import os
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "language_translation_bot"
BOT_NAME = "Language Translator"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "translation_config.json"
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

# ── Translator engines ───────────────────────────────────────────

class TranslatorEngine:
    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
        raise NotImplementedError

class GoogleTranslateEngine(TranslatorEngine):
    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
        from deep_translator import GoogleTranslator
        translator = GoogleTranslator(source=source_lang, target=target_lang)
        return translator.translate(text)

class MBartTranslateEngine(TranslatorEngine):
    def __init__(self, target_lang: str):
        from transformers import MBart50TokenizerFast, MBartForConditionalGeneration
        import torch
        self.tokenizer = MBart50TokenizerFast.from_pretrained("facebook/mbart-large-50-many-to-many-mmt")
        self.model = MBartForConditionalGeneration.from_pretrained("facebook/mbart-large-50-many-to-many-mmt")
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.target_lang = target_lang  # e.g., "en_XX"
        # Map from ISO-639-1 to MBart codes
        self.lang_code_map = {
            "auto": None,  # will use langdetect
            "en": "en_XX",
            "es": "es_XX",
            "fr": "fr_XX",
            "de": "de_DE",
            "it": "it_IT",
            "pt": "pt_XX",
            "nl": "nl_XX",
            "ru": "ru_RU",
            "zh": "zh_CN",
            "ja": "ja_XX",
            "ko": "ko_KR",
            "ar": "ar_AR",
            "tr": "tr_TR",
            "pl": "pl_PL",
            "ro": "ro_RO",
            # add more as needed
        }

    def translate(self, text: str, source_lang: str = "auto", target_lang: str = "en") -> str:
        import langdetect
        # Determine source language code
        if source_lang == "auto":
            try:
                detected = langdetect.detect(text)
            except Exception:
                detected = "en"  # fallback
        else:
            detected = source_lang
        src_code = self.lang_code_map.get(detected)
        if not src_code:
            # fallback to a common one if missing
            src_code = "en_XX"
        tgt_code = self.lang_code_map.get(target_lang, "en_XX")
        # Tokenize and translate
        self.tokenizer.src_lang = src_code
        encoded = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
        encoded = {k: v.to(self.device) for k, v in encoded.items()}
        generated_tokens = self.model.generate(
            **encoded,
            forced_bos_token_id=self.tokenizer.lang_code_to_id[tgt_code],
            max_length=512
        )
        result = self.tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)[0]
        return result

# ── Global engine instance ────────────────────────────────────────
translator: Optional[TranslatorEngine] = None

def init_translator(config: dict):
    global translator
    engine = config.get("engine", "google")
    target_lang = config.get("target_language", "en")
    if engine == "google":
        translator = GoogleTranslateEngine()
    elif engine == "mbart":
        translator = MBartTranslateEngine(target_lang)
    else:
        raise ValueError(f"Unknown engine: {engine}")

# ── HTTP API handler ─────────────────────────────────────────────
class TranslateHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/translate":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                text = data.get("text", "")
                source = data.get("source_lang", "auto")
                target = data.get("target_lang", "en")
                if not text:
                    self._respond(400, {"error": "Missing 'text' field"})
                    return
                translated = translator.translate(text, source, target)
                summary = f"Translated text (len {len(text)} → {len(translated)})"
                _post(summary, "info", {"original_snippet": text[:100], "translated_snippet": translated[:100]})
                self._respond(200, {"translated": translated, "source_lang": source, "target_lang": target})
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
    server = HTTPServer(("0.0.0.0", port), TranslateHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"Translation API listening on port {port}", "info")

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
            translated = translator.translate(text, source_lang="auto", target_lang="en")
            out_name = Path(fname).stem + "_translated.txt"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as out:
                out.write(translated)
            _post(f"Translated {fname} → {out_name}", "info", {"file": fname})
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    global translator
    _post("Language Translation Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        init_translator(config)
        port = int(config.get("http_port", 9570))
        start_http(port)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
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
