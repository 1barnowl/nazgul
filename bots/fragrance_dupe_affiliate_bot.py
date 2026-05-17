#!/usr/bin/env python3
"""
fragrance_dupe_affiliate_bot.py — Fragrance Dupe Affiliate Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Starts a tiny web server on localhost that asks the user for their
favourite expensive perfume and instantly recommends a highly‑rated
affordable dupe with a budget‑retailer link (Zara, Dossier).

Every recommendation is also posted to the BotController hub as an
event, so you see the conversation in the dashboard.

Requirements:
    pip install requests

Configuration:
    A file named `fragrance_dupe_config.json` will be created on first
    run. Add your affiliate parameters there (optional – the bot works
    out‑of‑the‑box with direct product links).
"""

import json
import os
import socket
import sys
import time
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

import requests

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "fragrance_dupe_affiliate"
BOT_NAME = "Fragrance Dupe Affiliate"

# ── Config file ─────────────────────────────────────────────────────────────
CFG_FILE = Path(__file__).with_name("fragrance_dupe_config.json")
DEFAULT_CONFIG = {
    "web_port": 5050,
    "affiliate": {
        "dossier_id": "",   # optional, e.g. "?ref=YOUR_ID"
        "zara_id": ""       # optional
    }
}

# ── Dupe database ───────────────────────────────────────────────────────────
# Real budget-friendly alternatives to luxury fragrances.
# Format: luxury name (lowercase) -> { "dupe_name": ..., "brand": ..., "url": ..., "affiliate_param": ... }
# URLs are real product pages. Affiliate param will be appended if configured.
DUPE_DB = {
    "baccarat rouge 540": {
        "dupe_name": "Red Temptation",
        "brand": "Zara",
        "url": "https://www.zara.com/us/en/red-temptation-p20120020.html",
        "affiliate_tag": "zara_id"
    },
    "chanel no 5": {
        "dupe_name": "Zara Applejuice",
        "brand": "Zara",
        "url": "https://www.zara.com/us/en/applejuice-p20120017.html",
        "affiliate_tag": "zara_id"
    },
    "jo malone wood sage & sea salt": {
        "dupe_name": "Woody Chestnut",
        "brand": "Dossier",
        "url": "https://dossier.co/products/woody-chestnut",
        "affiliate_tag": "dossier_id"
    },
    "le labo santal 33": {
        "dupe_name": "Santal 33 Inspiration",
        "brand": "Dossier",
        "url": "https://dossier.co/products/santal-33",
        "affiliate_tag": "dossier_id"
    },
    "tom ford lost cherry": {
        "dupe_name": "Ambery Cherry",
        "brand": "Dossier",
        "url": "https://dossier.co/products/ambery-cherry",
        "affiliate_tag": "dossier_id"
    },
    "creed aventus": {
        "dupe_name": "Zara Vibrant Leather",
        "brand": "Zara",
        "url": "https://www.zara.com/us/en/vibrant-leather-p20120019.html",
        "affiliate_tag": "zara_id"
    },
    "ysl black opium": {
        "dupe_name": "Ambery Vanilla",
        "brand": "Dossier",
        "url": "https://dossier.co/products/ambery-vanilla",
        "affiliate_tag": "dossier_id"
    },
    "dior sauvage": {
        "dupe_name": "Zara Man Silver",
        "brand": "Zara",
        "url": "https://www.zara.com/us/en/man-silver-p20120446.html",
        "affiliate_tag": "zara_id"
    },
    "mugler alien": {
        "dupe_name": "Floral Jasmine",
        "brand": "Dossier",
        "url": "https://dossier.co/products/floral-jasmine",
        "affiliate_tag": "dossier_id"
    },
    "killian love don't be shy": {
        "dupe_name": "Floral Marshmallow",
        "brand": "Dossier",
        "url": "https://dossier.co/products/floral-marshmallow",
        "affiliate_tag": "dossier_id"
    }
}

# ── Hub posting ─────────────────────────────────────────────────────────────
def post_to_hub(summary, level="info", payload=None):
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

def wait_for_hub():
    for _ in range(60):
        try:
            if requests.get(HUB, timeout=2).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)

# ── Tiny web server (chatbot interface) ────────────────────────────────────
HTML_PAGE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Fragrance Dupe Finder</title>
    <style>
        body {
            font-family: Arial, sans-serif;
            max-width: 600px;
            margin: 50px auto;
            padding: 20px;
            background: #fdf8f3;
            color: #333;
        }
        h1 {
            color: #8b3a3a;
        }
        input[type="text"] {
            width: 80%;
            padding: 10px;
            margin: 10px 0;
            border: 2px solid #ccc;
            border-radius: 6px;
            font-size: 16px;
        }
        button {
            padding: 10px 20px;
            background: #8b3a3a;
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 16px;
            cursor: pointer;
        }
        .result {
            margin-top: 20px;
            padding: 15px;
            background: #fff;
            border: 1px solid #ddd;
            border-radius: 6px;
        }
        .dupe-name {
            font-weight: bold;
            font-size: 1.2em;
        }
        .link a {
            color: #8b3a3a;
            text-decoration: underline;
        }
    </style>
</head>
<body>
    <h1>✨ Fragrance Dupe Finder</h1>
    <p>What’s your favourite expensive perfume?</p>
    <form action="/" method="GET">
        <input type="text" name="perfume" placeholder="e.g. Baccarat Rouge 540" autofocus />
        <button type="submit">Find My Dupe</button>
    </form>
    <div class="result" id="result">
        {result_html}
    </div>
</body>
</html>
"""

def build_result_html(recommendation, user_input):
    if not recommendation:
        return f'<p>Sorry, no dupe found for <strong>{user_input}</strong>. Try a different perfume.</p>'
    link = recommendation["final_url"]
    return f"""
        <p>For <strong>{user_input}</strong> we recommend:</p>
        <div class="dupe-name">{recommendation['dupe_name']}</div>
        <div>by <strong>{recommendation['brand']}</strong></div>
        <div class="link"><a href="{link}" target="_blank">Buy it here &rarr;</a></div>
        <p><em>Affiliate link – supports the bot.</em></p>
    """

class ChatHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            query = parse_qs(parsed.query).get("perfume", [None])[0]
            result = ""
            if query:
                rec = find_dupe(query, self.server.config)
                # Post to hub
                if rec:
                    post_to_hub(
                        f"User asked for '{query}' → Recommended {rec['dupe_name']} ({rec['brand']}) — {rec['final_url']}",
                        "info",
                        rec
                    )
                else:
                    post_to_hub(
                        f"User asked for '{query}' — no dupe found.",
                        "warning",
                        {"query": query}
                    )
                result = build_result_html(rec, query)
            else:
                result = ""
            html = HTML_PAGE.replace("{result_html}", result)
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
        else:
            self.send_error(404)
    def log_message(self, format, *args):
        pass  # suppress console spam

def find_dupe(user_input, config):
    """Look up the dupe for the given perfume name. Returns dict with dupe details and final URL including affiliate param if configured."""
    user_lower = user_input.strip().lower()
    # Direct match
    if user_lower in DUPE_DB:
        entry = DUPE_DB[user_lower]
    else:
        # Fuzzy match: check if any key is contained in user input or vice versa
        for key, val in DUPE_DB.items():
            if key in user_lower or user_lower in key:
                entry = val
                break
        else:
            return None
    # Build final URL with affiliate param
    aff_tag = entry.get("affiliate_tag", "")
    aff_id = ""
    if aff_tag and config.get("affiliate", {}).get(aff_tag):
        aff_id = config["affiliate"][aff_tag]
    url = entry["url"]
    if aff_id:
        sep = "&" if "?" in url else "?"
        final_url = f"{url}{sep}{aff_id}"
    else:
        final_url = url
    return {
        "dupe_name": entry["dupe_name"],
        "brand": entry["brand"],
        "url": entry["url"],
        "final_url": final_url
    }

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    # Load config
    if CFG_FILE.exists():
        with open(CFG_FILE, "r") as f:
            config = json.load(f)
    else:
        config = DEFAULT_CONFIG
        with open(CFG_FILE, "w") as f:
            json.dump(config, f, indent=2)

    port = config.get("web_port", 5050)

    # Start HTTP server
    server = HTTPServer(("127.0.0.1", port), ChatHandler)
    server.config = config  # attach config

    post_to_hub(
        f"Fragrance Dupe Bot online. Open http://localhost:{port} to find your dupe.",
        "info",
        {"url": f"http://localhost:{port}"}
    )

    # Optionally auto-open browser (friendly, but can be commented out)
    try:
        webbrowser.open(f"http://localhost:{port}")
    except Exception:
        pass

    # Serve forever, sending heartbeats every 20s
    def heartbeat_loop():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME,
                    "status": "online",
                }, timeout=3)
            except Exception:
                pass

    import threading
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

if __name__ == "__main__":
    main()
