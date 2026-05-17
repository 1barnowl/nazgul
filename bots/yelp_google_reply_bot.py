#!/usr/bin/env python3
"""
yelp_google_reply_bot.py — Yelp / Google Business Profile Reply Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Monitors Google reviews and Q&A via the Google Business Profile API
and auto‑replies with professional responses.  Yelp automated
replies are not supported by the public API – this bot handles
Google listings only.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client requests

Configuration
─────────────
Place `yelp_google_reply_config.json` in the same directory:

{
  "google": {
    "client_id": "YOUR_OAUTH_CLIENT_ID",
    "client_secret": "YOUR_OAUTH_CLIENT_SECRET",
    "refresh_token": "YOUR_REFRESH_TOKEN",
    "location_id": "locations/12345678901234567890",
    "account_id": "accounts/12345678901234567890"
  },
  "auto_reply": {
    "enabled": true,
    "review_template": "Thank you for your review, {reviewer_name}! We're thrilled to hear your feedback. We hope to see you again soon.",
    "qna_template": "Thank you for your question. Our team will get back to you shortly.",
    "llm": null
  },
  "state_file": "yelp_google_reply_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 60
}
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "yelp_google_reply_bot"
BOT_NAME = "Yelp / Google Business Profile Reply"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "yelp_google_reply_config.json"
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

# ── State management ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"replied_review_ids": [], "replied_qna_ids": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Google Business Profile API client ───────────────────────────
def get_gbp_service(config: dict):
    """Create and return an authenticated My Business API service (v4)."""
    google_cfg = config["google"]
    creds = Credentials(
        token=None,
        refresh_token=google_cfg["refresh_token"],
        token_uri="https://accounts.google.com/o/oauth2/token",
        client_id=google_cfg["client_id"],
        client_secret=google_cfg["client_secret"]
    )
    # Build the My Business Business Information and Reviews services
    # We'll build the discovery URL manually, or use googleapiclient.discovery.build with specific service names.
    # The My Business API v4 is composed of several sub‑services. For reviews, we use 'mybusinessaccountmanagement' and 'mybusinessbusinessinformation' for location details,
    # and 'mybusinessplaceactions'? Actually the reviews endpoint is part of My Business API v4.
    # The easiest is to use the discovery service to build the main `mybusiness` service:
    try:
        service = build("mybusinessaccountmanagement", "v1", credentials=creds)
    except Exception:
        service = build("mybusiness", "v4", credentials=creds)  # fallback
    return service

def get_reviews(service, account_id: str, location_id: str, page_size: int = 50) -> List[dict]:
    """Fetch the list of reviews for a location."""
    try:
        # The endpoint: accounts/{accountId}/locations/{locationId}/reviews
        reviews_service = service.accounts().locations().reviews()
        request = reviews_service.list(parent=f"{account_id}/{location_id}", pageSize=page_size)
        response = request.execute()
        return response.get("reviews", [])
    except Exception as e:
        _post(f"Failed to fetch reviews: {e}", "error")
        return []

def reply_to_review(service, account_id: str, location_id: str, review_id: str, comment: str) -> bool:
    """Reply to a specific review. Returns True on success."""
    try:
        reviews_service = service.accounts().locations().reviews()
        # The reply is done via a separate endpoint: /v4/{parent}/reviews/{reviewId}:reply
        # The parent is accounts/{account}/locations/{location}
        parent = f"{account_id}/{location_id}"
        request = reviews_service.reply(name=f"{parent}/reviews/{review_id}", body={"comment": comment})
        request.execute()
        return True
    except Exception as e:
        _post(f"Failed to reply to review {review_id}: {e}", "error")
        return False

def get_qna_questions(service, account_id: str, location_id: str) -> List[dict]:
    """Fetch unanswered Q&A questions."""
    # The Q&A API is: accounts/{account}/locations/{location}/questions
    # We'll use the 'mybusinessqanda' v1 API if available; otherwise, we can use the same service.
    try:
        qna_service = service.accounts().locations().questions()
        parent = f"{account_id}/{location_id}"
        request = qna_service.list(parent=parent, pageSize=20)
        response = request.execute()
        return response.get("questions", [])
    except AttributeError:
        _post("Q&A API not available in current Google service; skipping Q&A.", "warning")
        return []
    except Exception as e:
        _post(f"Failed to fetch Q&A: {e}", "error")
        return []

def answer_question(service, account_id: str, location_id: str, question_id: str, answer_text: str) -> bool:
    """Answer a Q&A question."""
    try:
        qna_service = service.accounts().locations().questions()
        parent = f"{account_id}/{location_id}"
        request = qna_service.answers().create(parent=f"{parent}/questions/{question_id}", body={"text": answer_text})
        request.execute()
        return True
    except Exception as e:
        _post(f"Failed to answer Q&A {question_id}: {e}", "error")
        return False

# ── Auto‑reply logic ─────────────────────────────────────────────
def process_reviews(config: dict, state: dict, service):
    """Reply to new reviews."""
    account = config["google"]["account_id"]
    location = config["google"]["location_id"]
    replies_cfg = config.get("auto_reply", {})
    if not replies_cfg.get("enabled"):
        return
    template = replies_cfg.get("review_template", "Thank you for your review!")
    replied_ids = set(state.get("replied_review_ids", []))

    reviews = get_reviews(service, account, location)
    for review in reviews:
        review_id = review.get("reviewId")
        if review_id in replied_ids:
            continue
        # Extract reviewer name
        reviewer_name = review.get("reviewer", {}).get("displayName", "there")
        comment = template.replace("{reviewer_name}", reviewer_name)
        # Optionally use LLM
        llm_cfg = replies_cfg.get("llm")
        if llm_cfg and llm_cfg.get("api_key"):
            import openai
            try:
                client = openai.OpenAI(api_key=llm_cfg["api_key"])
                prompt = f"Write a brief, professional response to this Google review by {reviewer_name}: '{review.get('comment','')}'. Make it friendly and thank them."
                resp = client.chat.completions.create(
                    model=llm_cfg.get("model", "gpt-4o-mini"),
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7,
                    max_tokens=200
                )
                comment = resp.choices[0].message.content.strip()
            except Exception as e:
                _post(f"LLM reply failed: {e}", "error")

        if reply_to_review(service, account, location, review_id, comment):
            _post(f"Replied to review {review_id} from {reviewer_name}", "info")
            replied_ids.add(review_id)
            state["replied_review_ids"] = list(replied_ids)[-500:]
            time.sleep(1)
        else:
            _post(f"Failed to reply to review {review_id}", "error")

def process_qna(config: dict, state: dict, service):
    """Answer new Q&A questions."""
    account = config["google"]["account_id"]
    location = config["google"]["location_id"]
    replies_cfg = config.get("auto_reply", {})
    if not replies_cfg.get("enabled"):
        return
    template = replies_cfg.get("qna_template", "Thank you for your question.")
    answered_ids = set(state.get("replied_qna_ids", []))

    questions = get_qna_questions(service, account, location)
    if not questions:
        return
    for q in questions:
        qid = q.get("name", "").split("/")[-1]  # extracts question ID
        if not qid or qid in answered_ids:
            continue
        answer = template
        if answer_question(service, account, location, qid, answer):
            _post(f"Answered Q&A question {qid}", "info")
            answered_ids.add(qid)
            state["replied_qna_ids"] = list(answered_ids)[-500:]
            time.sleep(1)
        else:
            _post(f"Failed to answer Q&A {qid}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Yelp / Google Business Profile Reply Bot online")
    _post("Note: Yelp automated replies are not supported by the public API. This bot handles Google listings only.", "info")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "yelp_google_reply_state.json")
        state = load_state(state_file)

        try:
            service = get_gbp_service(config)
        except Exception as e:
            _post(f"Failed to build Google service: {e}", "error")
            time.sleep(300)
            continue

        # Process reviews and Q&A
        process_reviews(config, state, service)
        process_qna(config, state, service)

        save_state(state_file, state)

        poll_minutes = int(config.get("poll_interval_minutes", 60))
        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
