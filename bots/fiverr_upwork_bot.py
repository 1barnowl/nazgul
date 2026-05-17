#!/usr/bin/env python3
"""
fiverr_upwork_bot.py — Fiverr / Upwork Service Offer Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auto‑responds to buyer requests with pre‑written proposals.
Upwork integration uses the official REST API. Fiverr uses
Playwright automation because no public proposal API exists.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests requests-oauthlib playwright
    playwright install chromium

Configuration
─────────────
Place `fiverr_upwork_config.json` in the same directory:

{
  "upwork": {
    "enabled": true,
    "client_id": "YOUR_CLIENT_ID",
    "client_secret": "YOUR_CLIENT_SECRET",
    "refresh_token": "YOUR_REFRESH_TOKEN",
    "callback_uri": "https://your-callback-url.com",
    "keywords": ["web scraping", "automation", "python script"],
    "proposal_template": "Hello! I'm an expert in {keyword}. I can deliver this quickly. Here's a recent example: https://your-portfolio.com",
    "max_proposals_per_run": 3,
    "cooldown_hours": 6
  },
  "fiverr": {
    "enabled": true,
    "email": "your_fiverr_email",
    "password": "your_fiverr_password",
    "headless": true,
    "keywords": ["web scraping", "automation"],
    "proposal_template": "I can help with your request. Check out my gig: https://www.fiverr.com/your-gig",
    "max_proposals_per_run": 2,
    "cooldown_hours": 8
  },
  "state_file": "fiverr_upwork_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 60
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
from requests_oauthlib import OAuth2Session

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "fiverr_upwork_bot"
BOT_NAME = "Fiverr / Upwork Service Offer"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "fiverr_upwork_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(f"{HUB}/ingest", json={
            "bot_id": BOT_ID,
            "bot_name": BOT_NAME,
            "summary": summary,
            "level": level,
            "payload": payload or {},
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
            "status": "online",
        }, timeout=3)
    except Exception:
        pass
    _last_hb = time.time()

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "upwork_last_run": None,
            "fiverr_last_run": None,
            "upwork_replied_jobs": [],
            "fiverr_replied_ids": []
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Upwork API integration ───────────────────────────────────────
UPWORK_BASE = "https://www.upwork.com"

def _upwork_get_access_token(config: dict) -> Optional[str]:
    """Obtain a fresh access token using the refresh token."""
    client_id = config["client_id"]
    client_secret = config["client_secret"]
    refresh_token = config["refresh_token"]
    callback_uri = config.get("callback_uri", "https://localhost")
    oauth = OAuth2Session(client_id, redirect_uri=callback_uri)
    token_url = f"{UPWORK_BASE}/api/v3/oauth2/token"
    try:
        token = oauth.refresh_token(token_url, refresh_token=refresh_token,
                                    client_id=client_id, client_secret=client_secret)
        return token.get("access_token")
    except Exception as e:
        _post(f"Upwork token refresh failed: {e}", "error")
        return None

def _upwork_get(path: str, access_token: str, params: dict = None) -> Optional[dict]:
    url = f"{UPWORK_BASE}/api/{path}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            _post(f"Upwork API GET {path} error: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Upwork request error: {e}", "error")
        return None

def _upwork_post(path: str, access_token: str, body: dict) -> Optional[dict]:
    url = f"{UPWORK_BASE}/api/{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            return resp.json()
        else:
            _post(f"Upwork API POST {path} error: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Upwork request error: {e}", "error")
        return None

def upwork_search_jobs(access_token: str, keyword: str, limit: int = 10) -> List[dict]:
    """Search for open jobs matching the keyword."""
    endpoint = "profiles/v2/search/jobs.json"
    params = {
        "q": keyword,
        "limit": limit,
        "sort": "create_time desc"
    }
    data = _upwork_get(endpoint, access_token, params)
    if data and "jobs" in data:
        return data["jobs"]
    return []

def upwork_submit_proposal(access_token: str, job_id: str, cover_letter: str) -> bool:
    """Submit a proposal to a job."""
    endpoint = "proposals/v2/proposals"
    body = {
        "jobId": job_id,
        "coverLetter": cover_letter
    }
    resp = _upwork_post(endpoint, access_token, body)
    return resp is not None

def process_upwork(config: dict, state: dict):
    upwork_cfg = config.get("upwork", {})
    if not upwork_cfg.get("enabled"):
        return
    access_token = _upwork_get_access_token(upwork_cfg)
    if not access_token:
        return

    # Rate limiting
    last_run_str = state.get("upwork_last_run")
    cooldown_hours = float(upwork_cfg.get("cooldown_hours", 6))
    if last_run_str:
        last_run = datetime.fromisoformat(last_run_str)
        if datetime.now(timezone.utc) - last_run < timedelta(hours=cooldown_hours):
            _post("Upwork: cooldown period active, skipping", "info")
            return

    keywords = upwork_cfg.get("keywords", [])
    max_proposals = int(upwork_cfg.get("max_proposals_per_run", 3))
    template = upwork_cfg.get("proposal_template", "")
    if not keywords or not template:
        _post("Upwork keywords/template missing", "warning")
        return

    already_replied = set(state.get("upwork_replied_jobs", []))
    proposals_sent = 0

    for keyword in keywords:
        jobs = upwork_search_jobs(access_token, keyword)
        for job in jobs:
            job_id = job.get("id")
            if job_id in already_replied:
                continue
            # Build cover letter
            title = job.get("title", keyword)
            cover_letter = template.replace("{keyword}", keyword).replace("{job_title}", title)
            success = upwork_submit_proposal(access_token, job_id, cover_letter)
            if success:
                _post(f"Upwork: proposal sent to job {job_id} ({title[:50]})", "info")
                already_replied.add(job_id)
                proposals_sent += 1
                time.sleep(1)
                if proposals_sent >= max_proposals:
                    break
            else:
                _post(f"Upwork: failed to submit proposal for job {job_id}", "error")
        if proposals_sent >= max_proposals:
            break

    state["upwork_replied_jobs"] = list(already_replied)[-500:]
    state["upwork_last_run"] = datetime.now(timezone.utc).isoformat()

# ── Fiverr integration (Playwright) ──────────────────────────────
def fiverr_login(page, email: str, password: str) -> bool:
    try:
        page.goto("https://www.fiverr.com/login", wait_until="networkidle")
        page.fill('input[name="email"]', email)
        page.fill('input[name="password"]', password)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")
        if "login" not in page.url:
            _post("Logged into Fiverr", "info")
            return True
        else:
            _post("Fiverr login failed", "error")
            return False
    except Exception as e:
        _post(f"Fiverr login error: {e}", "error")
        return False

def fiverr_get_buyer_requests(page, keyword: str, max_results: int = 5) -> List[dict]:
    """Search for buyer requests containing the keyword and return list of dicts with id and title."""
    # Fiverr's buyer requests page is at /requests. We'll navigate there.
    results = []
    try:
        page.goto("https://www.fiverr.com/requests", wait_until="networkidle")
        # Type keyword in the search box (if present)
        search_input = page.query_selector('input[placeholder*="Search requests"]')
        if search_input:
            search_input.fill(keyword)
            page.keyboard.press("Enter")
            page.wait_for_timeout(2000)
        # Gather request cards
        cards = page.query_selector_all('div.request-card')
        for card in cards:
            # Extract title and a unique identifier (href)
            title_el = card.query_selector('h3.request-title')
            link_el = card.query_selector('a[href*="/requests/"]')
            if title_el and link_el:
                href = link_el.get_attribute("href")
                req_id = href.split("/requests/")[-1] if "/requests/" in href else href
                results.append({
                    "id": req_id,
                    "title": title_el.inner_text().strip()
                })
                if len(results) >= max_results:
                    break
    except Exception as e:
        _post(f"Fiverr search error: {e}", "error")
    return results

def fiverr_send_offer(page, request_id: str, message: str) -> bool:
    """Navigate to a request detail page and send an offer."""
    try:
        page.goto(f"https://www.fiverr.com/requests/{request_id}", wait_until="networkidle")
        # Click "Send Offer"
        offer_btn = page.query_selector('button:has-text("Send Offer")')
        if not offer_btn:
            _post(f"Could not find Send Offer button for {request_id}", "error")
            return False
        offer_btn.click()
        page.wait_for_timeout(1000)
        # Fill the message field (textarea)
        message_field = page.query_selector('textarea[name="message"]')
        if not message_field:
            # try contenteditable
            message_field = page.query_selector('div[contenteditable="true"]')
        if not message_field:
            _post("No message field found", "error")
            return False
        message_field.click()
        message_field.type(message)
        # Submit
        submit_btn = page.query_selector('button[type="submit"], button:has-text("Send")')
        if submit_btn:
            submit_btn.click()
            page.wait_for_timeout(2000)
            return True
        return False
    except Exception as e:
        _post(f"Fiverr offer error: {e}", "error")
        return False

def process_fiverr(config: dict, state: dict):
    fiverr_cfg = config.get("fiverr", {})
    if not fiverr_cfg.get("enabled"):
        return
    # Rate limiting
    last_run_str = state.get("fiverr_last_run")
    cooldown_hours = float(fiverr_cfg.get("cooldown_hours", 8))
    if last_run_str:
        last_run = datetime.fromisoformat(last_run_str)
        if datetime.now(timezone.utc) - last_run < timedelta(hours=cooldown_hours):
            _post("Fiverr: cooldown period active, skipping", "info")
            return

    email = fiverr_cfg.get("email")
    password = fiverr_cfg.get("password")
    headless = fiverr_cfg.get("headless", True)
    keywords = fiverr_cfg.get("keywords", [])
    template = fiverr_cfg.get("proposal_template", "")
    max_proposals = int(fiverr_cfg.get("max_proposals_per_run", 2))
    if not keywords or not template:
        return

    from playwright.sync_api import sync_playwright
    already_replied = set(state.get("fiverr_replied_ids", []))
    proposals_sent = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = context.new_page()
        if not fiverr_login(page, email, password):
            browser.close()
            return

        for keyword in keywords:
            requests_list = fiverr_get_buyer_requests(page, keyword, max_results=5)
            for req in requests_list:
                if proposals_sent >= max_proposals:
                    break
                req_id = req["id"]
                if req_id in already_replied:
                    continue
                message = template.replace("{keyword}", keyword).replace("{request_title}", req["title"])
                success = fiverr_send_offer(page, req_id, message)
                if success:
                    _post(f"Fiverr: offer sent to request {req_id} ('{req['title'][:50]}')", "info")
                    already_replied.add(req_id)
                    proposals_sent += 1
                    time.sleep(2)
                else:
                    _post(f"Fiverr: failed to send offer to {req_id}", "error")
            if proposals_sent >= max_proposals:
                break
        browser.close()

    state["fiverr_replied_ids"] = list(already_replied)[-500:]
    state["fiverr_last_run"] = datetime.now(timezone.utc).isoformat()

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Fiverr / Upwork Service Offer Bot online")
    _post("Note: Fiverr automation uses browser automation and may be against ToS. Use at your own risk.", "warning")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "fiverr_upwork_state.json")
        state = load_state(state_file)

        process_upwork(config, state)
        process_fiverr(config, state)

        save_state(state_file, state)

        poll_min = int(config.get("poll_interval_minutes", 60))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
