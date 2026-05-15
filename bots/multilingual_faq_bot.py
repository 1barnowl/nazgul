#!/usr/bin/env python3
"""
multilingual_faq_bot.py — Multilingual FAQ & Knowledge Base Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Answers customer questions in multiple languages using a
centralised knowledge base. Matches questions via fuzzy string
matching and optionally translates answers using Google Translate
or a local MBart model.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install rapidfuzz requests deep-translator

Configuration
─────────────
Place `multilingual_faq_config.json` in the same directory:

{
  "knowledge_base_file": "faq_knowledge_base.json",
  "matching": {
    "method": "fuzzy",          // "fuzzy" or "keyword"
    "threshold": 80             // 0‑100 for fuzzy matching
  },
  "translation": {
    "enabled": true,
    "provider": "google",       // "google" or "mbart"
    "google": {
      "source": "auto",
      "target": "en"
    }
  },
  "http_port": 9660,
  "file_watch": {
    "enabled": false,
    "directory": "/data/faq_queries",
    "output_directory": "/data/faq_answers"
  },
  "heartbeat_interval": 30
}

Knowledge base file (`faq_knowledge_base.json`) format:
[
  {
    "language": "en",
    "question": "How do I reset my password?",
    "answer": "Go to settings and click 'Forgot Password'."
  },
  {
    "language": "es",
    "question": "¿Cómo cambio mi contraseña?",
    "answer": "Ve a configuración y haz clic en 'Olvidé mi contraseña'."
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
BOT_ID   = "multilingual_faq_bot"
BOT_NAME = "Multilingual FAQ Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "multilingual_faq_config.json"
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

# ── Translation engine (optional) ─────────────────────────────────
class BaseTranslator:
    def translate(self, text: str, source: str, target: str) -> str:
        raise NotImplementedError

class GoogleTranslator(BaseTranslator):
    def translate(self, text: str, source: str, target: str) -> str:
        from deep_translator import GoogleTranslator as GT
        translator = GT(source=source, target=target)
        return translator.translate(text)

class MbartTranslator(BaseTranslator):
    def __init__(self):
        from transformers import MBart50TokenizerFast, MBartForConditionalGeneration
        import torch
        model_name = "facebook/mbart-large-50-many-to-many-mmt"
        self.tokenizer = MBart50TokenizerFast.from_pretrained(model_name)
        self.model = MBartForConditionalGeneration.from_pretrained(model_name)
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        # map iso codes to mbart codes (simplified)
        self.lang_map = {
            "en": "en_XX", "es": "es_XX", "fr": "fr_XX", "de": "de_DE",
            "it": "it_IT", "pt": "pt_XX", "nl": "nl_XX", "ru": "ru_RU",
            "zh": "zh_CN", "ja": "ja_XX", "ko": "ko_KR", "ar": "ar_AR"
        }

    def translate(self, text: str, source: str, target: str) -> str:
        src_code = self.lang_map.get(source, "en_XX")
        tgt_code = self.lang_map.get(target, "en_XX")
        self.tokenizer.src_lang = src_code
        encoded = self.tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
        encoded = {k: v.to(self.device) for k, v in encoded.items()}
        generated_tokens = self.model.generate(
            **encoded,
            forced_bos_token_id=self.tokenizer.lang_code_to_id[tgt_code],
            max_length=512
        )
        return self.tokenizer.batch_decode(generated_tokens, skip_special_tokens=True)[0]

translator_instance: Optional[BaseTranslator] = None

def init_translator(config: dict):
    global translator_instance
    if not config.get("translation", {}).get("enabled"):
        return
    provider = config["translation"].get("provider", "google")
    if provider == "google":
        translator_instance = GoogleTranslator()
    elif provider == "mbart":
        translator_instance = MbartTranslator()
    else:
        _post(f"Unknown translator provider: {provider}", "warning")

def maybe_translate(text: str, from_lang: str, to_lang: str) -> str:
    if not translator_instance or from_lang == to_lang:
        return text
    try:
        return translator_instance.translate(text, from_lang, to_lang)
    except Exception as e:
        _post(f"Translation failed ({from_lang}->{to_lang}): {e}", "warning")
        return text  # fallback to original

# ── Knowledge base ───────────────────────────────────────────────
knowledge_base: List[Dict] = []
threshold: int = 80

def load_kb(path: str) -> List[Dict]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
            if isinstance(data, list):
                return data
    except Exception:
        pass
    return []

def find_best_match(question: str, language: str) -> Optional[Dict]:
    from rapidfuzz import process, fuzz
    # filter entries by language first
    lang_entries = [e for e in knowledge_base if e.get("language") == language]
    if not lang_entries:
        # fallback to English
        lang_entries = [e for e in knowledge_base if e.get("language") == "en"]
        if not lang_entries:
            return None
        # indicate we might need translation later
    questions = [e["question"] for e in lang_entries]
    match = process.extractOne(question, questions, scorer=fuzz.token_sort_ratio, score_cutoff=threshold)
    if match:
        best_text, score, idx = match
        return lang_entries[idx]
    return None

# ── HTTP API handler ─────────────────────────────────────────────
class FAQHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/ask":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                question = data.get("question", "")
                language = data.get("language", "en")
                if not question:
                    self._respond(400, {"error": "Missing 'question'"})
                    return
                # Find best match
                entry = find_best_match(question, language)
                if entry:
                    answer_text = entry["answer"]
                    if entry.get("language") != language:
                        # translate answer if needed
                        answer_text = maybe_translate(answer_text, entry["language"], language)
                else:
                    answer_text = "Sorry, I don't have an answer for that."
                summary = f"Q({language}): {question[:60]}... → A: {answer_text[:60]}..."
                _post(summary, "info", {"question": question, "language": language, "answer": answer_text})
                self._respond(200, {"answer": answer_text})
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
    server = HTTPServer(("0.0.0.0", port), FAQHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"FAQ API listening on port {port}", "info")

# ── File watcher (optional) ─────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set):
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
            question = data.get("question", "")
            language = data.get("language", "en")
            entry = find_best_match(question, language)
            if entry:
                answer_text = entry["answer"]
                if entry.get("language") != language:
                    answer_text = maybe_translate(answer_text, entry["language"], language)
            else:
                answer_text = "Sorry, no answer found."
            result = {"question": question, "language": language, "answer": answer_text}
            out_name = Path(fname).stem + "_answer.json"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w") as out:
                json.dump(result, out, indent=2)
            _post(f"Processed {fname} → {out_name}", "info")
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    global knowledge_base, threshold
    _post("Multilingual FAQ Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        # Load knowledge base
        kb_path = config.get("knowledge_base_file", "faq_knowledge_base.json")
        knowledge_base = load_kb(kb_path)
        threshold = int(config.get("matching", {}).get("threshold", 80))

        # Initialise translator
        init_translator(config)

        port = int(config.get("http_port", 9660))
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
