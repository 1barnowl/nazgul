#!/usr/bin/env python3
"""
webhook_listener_bot.py — Webhook Listener Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
HTTP server that accepts incoming JSON payloads from
third‑party services (Stripe, GitHub, Shopify, etc.)
and forwards them to the Nazgul BotController.

Attachable to the BotController dashboard (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `webhook_listener_config.json` in the same directory:

{
  "port": 9500,
  "endpoints": [
    {
      "path": "/stripe",
      "provider": "stripe",
      "secret": "whsec_...",
      "signature_header": "Stripe-Signature"
    },
    {
      "path": "/github",
      "provider": "github",
      "secret": "my-secret",
      "signature_header": "X-Hub-Signature-256"
    }
  ],
  "heartbeat_interval": 30
}
"""

import json
import hashlib
import hmac
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "webhook_listener_bot"
BOT_NAME = "Webhook Listener"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "webhook_listener_config.json"
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

# ── Signature verification ───────────────────────────────────────
def verify_signature(body: bytes, headers: dict, secret: str,
                     provider: str, signature_header: Optional[str]) -> bool:
    """
    Verify webhook signature based on provider conventions.
    Returns True if valid (or if secret not set).
    """
    if not secret:
        return True  # no verification configured

    sig = None
    if signature_header:
        sig = headers.get(signature_header)
    if not sig:
        return False

    if provider == "github":
        # GitHub uses HMAC SHA-256, header format "sha256=<hash>"
        if not sig.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig[7:], expected)

    elif provider == "shopify":
        # Shopify uses HMAC SHA-256, header X-Shopify-Hmac-Sha256
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)

    elif provider == "stripe":
        # Stripe signature: "t=timestamp,v1=signature" using HMAC SHA-256
        try:
            # Basic implementation; for production, use stripe-python
            parts = dict(item.split("=", 1) for item in sig.split(","))
            t = parts.get("t")
            v1 = parts.get("v1")
            if not t or not v1:
                return False
            signed_payload = f"{t}.{body.decode()}"
            expected = hmac.new(secret.encode(), signed_payload.encode(),
                                hashlib.sha256).hexdigest()
            return hmac.compare_digest(v1, expected)
        except Exception:
            return False

    # Fallback: assume HMAC SHA-256 raw hex
    try:
        expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(sig, expected)
    except Exception:
        return False

# ── HTTP request handler ─────────────────────────────────────────
class WebhookHandler(BaseHTTPRequestHandler):
    endpoints_config: list = []   # set from config before serving

    def do_POST(self) -> None:
        path = self.path.rstrip("/")
        # Find matching endpoint config
        endpoint_cfg = None
        for ep in self.endpoints_config:
            if ep.get("path", "").rstrip("/") == path:
                endpoint_cfg = ep
                break
        if not endpoint_cfg:
            self._respond(404, {"error": "endpoint not configured"})
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        # Verify signature
        secret = endpoint_cfg.get("secret")
        provider = endpoint_cfg.get("provider", "generic")
        sig_header = endpoint_cfg.get("signature_header")
        headers_lower = {k.lower(): v for k, v in self.headers.items()}
        if sig_header:
            sig_header_lower = sig_header.lower()
            sig_value = headers_lower.get(sig_header_lower)
            if sig_value:
                # rebuild headers with correct case?
                pass

        if not verify_signature(body, dict(self.headers), secret, provider, sig_header):
            self._respond(401, {"error": "invalid signature"})
            return

        # Parse body as JSON
        try:
            payload = json.loads(body)
        except Exception:
            self._respond(400, {"error": "invalid JSON"})
            return

        # Build summary
        provider_name = endpoint_cfg.get("provider", "generic").capitalize()
        path_name = path.strip("/")
        summary = f"{provider_name} webhook received on /{path_name}"
        # Optionally include a useful field from payload
        if provider == "github":
            event = self.headers.get("X-GitHub-Event", "")
            summary += f" (event: {event})"
        elif provider == "stripe":
            obj_type = payload.get("type", "")
            summary += f" (type: {obj_type})"
        elif provider == "shopify":
            obj_type = payload.get("id", "")
            summary += f" (order id: {obj_type})"

        # Forward to hub
        _post(summary, "info", {
            "provider": provider,
            "path": path,
            "payload": payload
        })
        self._respond(200, {"status": "ok"})

    def _respond(self, code: int, data: dict) -> None:
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass

# ── Main loop ────────────────────────────────────────────────────
def main() -> None:
    _post("Webhook Listener Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        port = int(config.get("port", 9500))
        endpoints = config.get("endpoints", [])
        WebhookHandler.endpoints_config = endpoints

        server = HTTPServer(("0.0.0.0", port), WebhookHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        _post(f"Webhook server started on port {port}", "info")

        # Keep main thread alive, report heartbeats
        while True:
            _heartbeat()
            time.sleep(10)

if __name__ == "__main__":
    main()
