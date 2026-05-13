#!/usr/bin/env python3
"""
pdf_image_ocr_bot.py — PDF/Image OCR Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Extracts text from scanned documents and screenshots
using Tesseract, TrOCR, or a Vision LLM. Exposes an
HTTP API and optional file watcher.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install pytesseract Pillow pdf2image requests
    For TrOCR: pip install transformers torch
    System: tesseract-ocr (if using Tesseract engine)

Configuration
─────────────
Place `ocr_config.json` in the same directory:

{
  "engine": "tesseract",                // "tesseract", "trocr", or "openai_vision"
  "tesseract": {
    "cmd": "tesseract",                 // path to executable if not in PATH
    "lang": "eng"
  },
  "trocr": {
    "model_name": "microsoft/trocr-base-printed"
  },
  "openai_vision": {
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "endpoint": null
  },
  "http_port": 9560,
  "file_watch": {
    "enabled": false,
    "directory": "/data/scanned",
    "output_directory": "/data/ocr_output"
  },
  "max_file_size_mb": 20
}
"""

import json
import os
import base64
import io
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "pdf_image_ocr_bot"
BOT_NAME = "PDF/Image OCR"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "ocr_config.json"
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

# ── OCR Engine Interface ─────────────────────────────────────────

class OCREngine:
    def ocr_file(self, file_bytes: bytes, filename: str) -> str:
        raise NotImplementedError

class TesseractEngine(OCREngine):
    def __init__(self, config: dict):
        import pytesseract
        from PIL import Image
        from pdf2image import convert_from_bytes
        self.pytesseract = pytesseract
        self.Image = Image
        self.convert_from_bytes = convert_from_bytes
        tesseract_cmd = config.get("cmd", "tesseract")
        if tesseract_cmd != "tesseract":
            pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
        self.lang = config.get("lang", "eng")

    def ocr_file(self, file_bytes: bytes, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        images = []
        if ext == ".pdf":
            images = self.convert_from_bytes(file_bytes)
        elif ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif"):
            img = self.Image.open(io.BytesIO(file_bytes))
            images = [img]
        else:
            raise ValueError(f"Unsupported file type: {ext}")
        texts = []
        for img in images:
            text = self.pytesseract.image_to_string(img, lang=self.lang)
            texts.append(text)
        return "\n\n".join(texts)

class TrOCREngine(OCREngine):
    def __init__(self, config: dict):
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel
        from PIL import Image
        from pdf2image import convert_from_bytes
        import torch
        self.processor = TrOCRProcessor.from_pretrained(config.get("model_name", "microsoft/trocr-base-printed"))
        self.model = VisionEncoderDecoderModel.from_pretrained(config.get("model_name", "microsoft/trocr-base-printed"))
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.model.to(self.device)
        self.Image = Image
        self.convert_from_bytes = convert_from_bytes

    def ocr_file(self, file_bytes: bytes, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        images = []
        if ext == ".pdf":
            images = self.convert_from_bytes(file_bytes)
        elif ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif"):
            img = self.Image.open(io.BytesIO(file_bytes)).convert("RGB")
            images = [img]
        else:
            raise ValueError(f"Unsupported file type: {ext}")
        texts = []
        for img in images:
            pixel_values = self.processor(img, return_tensors="pt").pixel_values.to(self.device)
            generated_ids = self.model.generate(pixel_values)
            text = self.processor.batch_decode(generated_ids, skip_special_tokens=True)[0]
            texts.append(text)
        return "\n\n".join(texts)

class OpenAIVisionEngine(OCREngine):
    def __init__(self, config: dict):
        self.api_key = config["api_key"]
        self.model = config.get("model", "gpt-4o-mini")
        self.endpoint = config.get("endpoint") or "https://api.openai.com/v1/chat/completions"

    def ocr_file(self, file_bytes: bytes, filename: str) -> str:
        ext = Path(filename).suffix.lower()
        # Convert image to base64
        import base64 as b64
        if ext == ".pdf":
            # Convert first page of PDF to image using pdf2image
            from pdf2image import convert_from_bytes
            from PIL import Image
            images = convert_from_bytes(file_bytes)
            if not images:
                return ""
            img = images[0]
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64_data = b64.b64encode(buf.getvalue()).decode("utf-8")
        elif ext in (".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif"):
            from PIL import Image
            img = Image.open(io.BytesIO(file_bytes)).convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64_data = b64.b64encode(buf.getvalue()).decode("utf-8")
        else:
            raise ValueError(f"Unsupported file type: {ext}")
        payload = {
            "model": self.model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Please extract all text from this image. Return only the extracted text, no commentary."},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{b64_data}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 2000
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=30)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"]
        else:
            raise Exception(f"OpenAI Vision error: {resp.status_code} {resp.text}")

# ── Initialize global engine ─────────────────────────────────────

ocr_engine: OCREngine = None

def init_ocr_engine(config: dict):
    global ocr_engine
    engine_name = config.get("engine", "tesseract")
    if engine_name == "tesseract":
        ocr_engine = TesseractEngine(config.get("tesseract", {}))
    elif engine_name == "trocr":
        ocr_engine = TrOCREngine(config.get("trocr", {}))
    elif engine_name == "openai_vision":
        ocr_engine = OpenAIVisionEngine(config.get("openai_vision", {}))
    else:
        raise ValueError(f"Unsupported engine: {engine_name}")

# ── HTTP API handler ─────────────────────────────────────────────
class OCRHandler(BaseHTTPRequestHandler):
    max_size_mb = 20

    def do_POST(self):
        if self.path == "/ocr":
            content_type = self.headers.get("Content-Type", "")
            if "multipart/form-data" not in content_type:
                self._respond(400, {"error": "Use multipart/form-data with a file field 'file'"})
                return
            # Parse multipart form data manually (simple approach)
            boundary = content_type.split("boundary=")[-1]
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            # Crude multipart parser - only for single file named 'file'
            try:
                from email.parser import BytesParser
                from io import BytesIO
                import email.policy
                msg = BytesParser(policy=email.policy.HTTP).parsebytes(
                    b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body
                )
                for part in msg.walk():
                    if part.get_content_disposition() == "attachment" or (part.get_filename() or part.get_param("name") == "file"):
                        filename = part.get_filename() or part.get_param("name", "upload")
                        file_data = part.get_payload(decode=True)
                        if len(file_data) > self.max_size_mb * 1024 * 1024:
                            self._respond(413, {"error": f"File exceeds {self.max_size_mb} MB limit"})
                            return
                        text = ocr_engine.ocr_file(file_data, filename)
                        _post(f"OCR processed {filename}: {len(text)} chars", "info", {"filename": filename})
                        self._respond(200, {"text": text})
                        return
                self._respond(400, {"error": "No file part found"})
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

def start_http(port: int, max_size_mb: int):
    OCRHandler.max_size_mb = max_size_mb
    server = HTTPServer(("0.0.0.0", port), OCRHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _post(f"OCR API listening on port {port}", "info")

# ── File watcher ─────────────────────────────────────────────────
def watch_directory(directory: str, output_dir: str, processed: set, max_size_mb: int):
    try:
        entries = os.listdir(directory)
    except FileNotFoundError:
        return
    for fname in entries:
        ext = Path(fname).suffix.lower()
        supported = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".bmp", ".gif"}
        if ext not in supported:
            continue
        fpath = os.path.join(directory, fname)
        if fpath in processed:
            continue
        try:
            size_mb = os.path.getsize(fpath) / (1024 * 1024)
            if size_mb > max_size_mb:
                _post(f"Skipping {fname} ({size_mb:.1f}MB > limit)", "warning")
                processed.add(fpath)
                continue
            with open(fpath, "rb") as f:
                file_data = f.read()
            text = ocr_engine.ocr_file(file_data, fname)
            out_name = Path(fname).stem + ".txt"
            out_path = os.path.join(output_dir, out_name)
            with open(out_path, "w", encoding="utf-8") as out:
                out.write(text)
            _post(f"Processed {fname} -> {out_name} ({len(text)} chars)", "info", {"filename": fname})
            processed.add(fpath)
        except Exception as e:
            _post(f"Error processing {fname}: {e}", "warning")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("PDF/Image OCR Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        try:
            init_ocr_engine(config)
        except Exception as e:
            _post(f"Failed to initialize OCR engine: {e}", "error")
            time.sleep(60)
            continue

        port = int(config.get("http_port", 9560))
        max_size_mb = float(config.get("max_file_size_mb", 20))
        start_http(port, max_size_mb)

        file_cfg = config.get("file_watch", {})
        if file_cfg.get("enabled"):
            directory = file_cfg.get("directory")
            output_dir = file_cfg.get("output_directory")
            if directory and output_dir:
                os.makedirs(output_dir, exist_ok=True)
                processed_cache = set()
                while True:
                    watch_directory(directory, output_dir, processed_cache, max_size_mb)
                    _heartbeat()
                    time.sleep(10)
        else:
            while True:
                _heartbeat()
                time.sleep(10)

if __name__ == "__main__":
    main()
