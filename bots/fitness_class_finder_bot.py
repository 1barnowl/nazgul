#!/usr/bin/env python3
"""
fitness_class_finder_bot.py — Fitness Class Pass Finder Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Searches for local (or online) yoga / Pilates classes with intro
offers via the ClassPass platform. Every class listing includes an
affiliate link – you earn a referral fee when someone signs up.

Uses a web form to ask for city and activity, then attempts to
fetch real‑time class results from ClassPass’s public search API.
If scraping fails, it provides a direct ClassPass sign‑up link
(which still tracks your affiliate ID).

Requirements:
    pip install requests beautifulsoup4 lxml

Configuration:
    A file named `fitness_finder_config.json` is created on first run.
    Sign up for the ClassPass affiliate program (Impact/Partnerize) and
    replace `classpass_affiliate_id` with your tracking code (e.g., the
    `ref` parameter you received).
"""

import json
import time
import threading
import webbrowser
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse, quote

import requests
from bs4 import BeautifulSoup

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "fitness_class_finder"
BOT_NAME = "Fitness Class Pass Finder"

CFG_FILE = Path(__file__).with_name("fitness_finder_config.json")
DEFAULT_CONFIG = {
    "web_port": 5060,
    "classpass_affiliate_id": "",          # e.g. "123456" from Impact
    "search_radius_miles": 10
}

# ── Hub helpers ─────────────────────────────────────────────────────────────
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

# ── ClassPass search ────────────────────────────────────────────────────────
# ClassPass uses a GraphQL endpoint for search; we'll mimic the public query.
CLASSPASS_API = "https://classpass.com/api/search/search"

def search_classpass(city, activity, affiliate_id):
    """
    Attempt to hit the ClassPass search API and return a list of
    class dicts: {studio, class_name, time, link (with affiliate)}.
    Returns empty list on failure.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Referer": "https://classpass.com/search",
        "Origin": "https://classpass.com",
    }
    # The query mimics what their frontend sends; this may change.
    payload = {
        "query": activity,
        "location": city,
        "radius": 10,
        "categories": [activity.lower()],
        "page": 1,
        "pageSize": 10,
    }
    try:
        r = requests.post(CLASSPASS_API, json=payload, headers=headers, timeout=10)
        r.raise_for_status()
        data = r.json()
        # The actual structure may vary; we try to extract from common keys.
        results = data.get("data", {}).get("search", {}).get("classes", [])
        if not results:
            results = data.get("classes", []) or data.get("results", [])
        if not results:
            # If the API fails, we return empty – fallback to generic link.
            return []
        classes = []
        for item in results:
            studio = item.get("studio", {}).get("name", "Unknown Studio")
            class_name = item.get("name", item.get("className", "Class"))
            time_str = item.get("time", item.get("startTime", ""))
            # Build affiliate link: base URL + ?ref=affiliate_id
            class_slug = item.get("slug", "")
            if class_slug:
                detail_url = f"https://classpass.com/classes/{class_slug}"
            else:
                # fallback to studio page
                studio_slug = item.get("studio", {}).get("slug", "")
                detail_url = f"https://classpass.com/studios/{studio_slug}" if studio_slug else "https://classpass.com/search"
            if affiliate_id.strip():
                sep = "&" if "?" in detail_url else "?"
                detail_url += f"{sep}ref={affiliate_id.strip()}"
            classes.append({
                "studio": studio,
                "class_name": class_name,
                "time": time_str,
                "url": detail_url
            })
        return classes
    except Exception:
        return []

# ── Web interface ───────────────────────────────────────────────────────────
HTML = """
<!DOCTYPE html>
<html>
<head>
  <meta charset="UTF-8">
  <title>Fitness Class Pass Finder</title>
  <style>
    body { font-family:Arial; max-width:600px; margin:40px auto; background:#f5f7fa; color:#222; }
    h1 { color:#2c5f8a; }
    .card { background:#fff; padding:20px; margin:15px 0; border-radius:8px; box-shadow:0 2px 8px rgba(0,0,0,0.05); }
    label { font-weight:bold; display:block; margin:10px 0 4px; }
    input, select { width:100%; padding:10px; border:1px solid #ccc; border-radius:4px; }
    button { background:#2c5f8a; color:white; padding:10px 20px; border:none; border-radius:6px;
             font-size:16px; cursor:pointer; width:100%; margin-top:15px; }
    .class-card { background:#eef4ff; padding:12px; margin:10px 0; border-left:4px solid #2c5f8a; border-radius:4px; }
    .class-card a { color:#2c5f8a; font-weight:bold; }
    .fallback { margin-top:20px; font-style:italic; }
  </style>
</head>
<body>
<h1>🧘‍♀️ Find Yoga & Pilates Intro Offers</h1>
<p>Enter your city and what you're looking for. We'll search ClassPass for introductory deals and give you a referral link.</p>
<form method="GET" action="/">
  <div class="card">
    <label>City / Zip:</label>
    <input type="text" name="city" placeholder="e.g. San Francisco" required>
    <label>Activity:</label>
    <select name="activity">
      <option value="yoga">Yoga</option>
      <option value="pilates">Pilates</option>
      <option value="yoga,pilates">Both</option>
    </select>
    <button type="submit">Find Classes</button>
  </div>
</form>
<div id="results">{results}</div>
</body>
</html>
"""

class FinderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/":
            q = parse_qs(parsed.query)
            results_html = ""
            if "city" in q and "activity" in q:
                city = q["city"][0].strip()
                activities = q["activity"][0].split(",")
                affiliate_id = self.server.config.get("classpass_affiliate_id", "")
                all_classes = []
                for act in activities:
                    act = act.strip()
                    classes = search_classpass(city, act, affiliate_id)
                    all_classes.extend(classes)
                    time.sleep(0.5)
                if all_classes:
                    # Deduplicate by URL
                    seen = set()
                    unique = []
                    for c in all_classes:
                        if c["url"] not in seen:
                            seen.add(c["url"])
                            unique.append(c)
                    results_html += f"<h2>Found {len(unique)} class(es) near {city}</h2>"
                    for c in unique:
                        results_html += f"""
                        <div class="class-card">
                          <strong>{c['class_name']}</strong> at <em>{c['studio']}</em><br/>
                          <small>{c['time']}</small><br/>
                          <a href="{c['url']}" target="_blank">View & Sign Up (affiliate link) →</a>
                        </div>"""
                    post_to_hub(
                        f"🏋️ Found {len(unique)} classes for '{city}' ({', '.join(activities)})",
                        "info",
                        {"city": city, "activities": activities, "count": len(unique)}
                    )
                else:
                    # Fallback: provide a direct ClassPass sign‑up link with affiliate
                    base = "https://classpass.com/referral"
                    aff_param = f"?ref={affiliate_id}" if affiliate_id else ""
                    fallback_url = base + aff_param
                    results_html = f"""
                    <p>Couldn't fetch live classes right now.</p>
                    <p class="fallback">👉 <a href="{fallback_url}" target="_blank">Join ClassPass with our referral link</a>
                    and browse local yoga/Pilates intro offers yourself.</p>"""
                    post_to_hub(
                        f"⚠️ Live search failed for {city} – provided generic affiliate link.",
                        "warning",
                        {"fallback_url": fallback_url}
                    )
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            html_out = HTML.replace("{results}", results_html)
            self.wfile.write(html_out.encode())
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass

def start_server(config):
    port = config.get("web_port", 5060)
    server = HTTPServer(("127.0.0.1", port), FinderHandler)
    server.config = config
    post_to_hub(f"🧘 Fitness Class Finder live at http://localhost:{port}", "info")
    webbrowser.open(f"http://localhost:{port}")
    server.serve_forever()

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config file created at {CFG_FILE}. Add your ClassPass affiliate ID to earn referral fees.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    def heartbeat_loop():
        while True:
            time.sleep(20)
            try:
                requests.post(f"{HUB}/heartbeat/{BOT_ID}", json={
                    "bot_name": BOT_NAME, "status": "online",
                }, timeout=3)
            except Exception:
                pass
    threading.Thread(target=heartbeat_loop, daemon=True).start()

    start_server(config)

if __name__ == "__main__":
    main()
