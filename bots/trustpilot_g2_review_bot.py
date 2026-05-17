#!/usr/bin/env python3
"""
trustpilot_g2_review_bot.py — Trustpilot / G2 Review Management Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Fetches reviews from Trustpilot and G2, auto‑replies to unanswered
ones, sends review invitations to satisfied customers, and flags
reviews that mention competitors.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `trustpilot_g2_config.json` in the same directory:

{
  "trustpilot": {
    "enabled": true,
    "api_key": "YOUR_TRUSTPILOT_API_KEY",
    "api_secret": "YOUR_TRUSTPILOT_API_SECRET",
    "business_unit_id": "1234567890abcdef",
    "auto_reply": {
      "template": "Thank you for your review, {reviewer_name}! We appreciate your feedback.",
      "llm": null
    },
    "review_invitation": {
      "enabled": true,
      "template_id": "123456",
      "source_file": "trustpilot_customers_to_invite.json"
    }
  },
  "g2": {
    "enabled": true,
    "api_token": "YOUR_G2_API_TOKEN",
    "product_id": "my-product",
    "auto_reply": {
      "template": "Thanks for your review! Your insights help us improve.",
      "llm": null
    },
    "review_invitation": {
      "enabled": true,
      "source_file": "g2_customers_to_invite.json"
    }
  },
  "competitor_flags": {
    "enabled": true,
    "keywords": ["competitor1", "competitor2"]
  },
  "state_file": "trustpilot_g2_state.json",
  "heartbeat_interval": 30,
  "poll_interval_minutes": 60
}

Invitation files are JSON arrays of objects:
[
  {"email": "customer@example.com", "name": "John Doe"},
  ...
]

For Trustpilot invitations, you need a pre‑approved email template ID from the Trustpilot system.
"""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "trustpilot_g2_review_bot"
BOT_NAME = "Trustpilot / G2 Review Management"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "trustpilot_g2_config.json"
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
            "trustpilot_replied_review_ids": [],
            "g2_replied_review_ids": [],
            "trustpilot_invited_emails": [],
            "g2_invited_emails": []
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Trustpilot API ───────────────────────────────────────────────
TRUSTPILOT_API = "https://api.trustpilot.com/v1"

def trustpilot_get_access_token(api_key: str, api_secret: str) -> Optional[str]:
    """Obtain an OAuth2 access token using client credentials."""
    url = "https://api.trustpilot.com/v1/oauth/oauth-business-users-for-applications/accesstoken"
    data = {"grant_type": "client_credentials"}
    try:
        resp = requests.post(url, data=data, auth=(api_key, api_secret), timeout=10)
        if resp.status_code == 200:
            return resp.json()["access_token"]
        else:
            _post(f"Trustpilot token error: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"Trustpilot token request failed: {e}", "error")
        return None

def trustpilot_get_reviews(access_token: str, business_unit_id: str,
                           per_page: int = 50) -> List[dict]:
    """Fetch reviews for a business unit."""
    url = f"{TRUSTPILOT_API}/business-units/{business_unit_id}/reviews"
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"perPage": per_page}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("reviews", [])
        else:
            _post(f"Trustpilot reviews error: {resp.status_code} {resp.text[:200]}", "error")
            return []
    except Exception as e:
        _post(f"Trustpilot reviews request failed: {e}", "error")
        return []

def trustpilot_reply_to_review(access_token: str, review_id: str, message: str) -> bool:
    """Reply to a review."""
    url = f"{TRUSTPILOT_API}/reviews/{review_id}/reply"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {"message": message}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        _post(f"Trustpilot reply error: {e}", "error")
        return False

def trustpilot_send_invitation(access_token: str, business_unit_id: str,
                               template_id: str, email: str, name: str) -> bool:
    """Send a review invitation via Trustpilot."""
    url = f"{TRUSTPILOT_API}/business-units/{business_unit_id}/invitations"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    body = {
        "templateId": template_id,
        "recipient": {
            "email": email,
            "name": name
        }
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        return resp.status_code == 200
    except Exception as e:
        _post(f"Trustpilot invitation error: {e}", "error")
        return False

# ── G2 API ───────────────────────────────────────────────────────
G2_API = "https://data.g2.com/api/v1"

def g2_get_reviews(api_token: str, product_id: str, page: int = 1,
                   per_page: int = 50) -> List[dict]:
    """Fetch reviews for a G2 product."""
    url = f"{G2_API}/products/{product_id}/reviews"
    headers = {
        "Authorization": f"Token token={api_token}",
        "Content-Type": "application/json"
    }
    params = {"page[number]": page, "page[size]": per_page}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            return resp.json().get("data", [])
        else:
            _post(f"G2 reviews error: {resp.status_code} {resp.text[:200]}", "error")
            return []
    except Exception as e:
        _post(f"G2 reviews request failed: {e}", "error")
        return []

def g2_reply_to_review(api_token: str, review_id: str, message: str) -> bool:
    """Reply to a G2 review (via its comment endpoint)."""
    # G2 uses the comments resource under a review
    url = f"{G2_API}/reviews/{review_id}/comments"
    headers = {
        "Authorization": f"Token token={api_token}",
        "Content-Type": "application/json"
    }
    body = {"comment": {"body": message}}
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        return resp.status_code == 201
    except Exception as e:
        _post(f"G2 reply error: {e}", "error")
        return False

def g2_send_invitation(api_token: str, product_id: str, email: str, name: str) -> bool:
    """Send a review invitation via G2."""
    url = f"{G2_API}/review_invitations"
    headers = {
        "Authorization": f"Token token={api_token}",
        "Content-Type": "application/json"
    }
    body = {
        "review_invitation": {
            "product_id": product_id,
            "email": email,
            "name": name
        }
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=10)
        return resp.status_code == 201
    except Exception as e:
        _post(f"G2 invitation error: {e}", "error")
        return False

# ── Competitor flagging ──────────────────────────────────────────
def flag_competitor_mentions(review_text: str, keywords: List[str]) -> Optional[str]:
    """Return the first competitor keyword found in the review."""
    text_lower = review_text.lower()
    for kw in keywords:
        if kw.lower() in text_lower:
            return kw
    return None

# ── Main processing logic ────────────────────────────────────────
def process_trustpilot(config: dict, state: dict):
    """Handle Trustpilot: reply to new reviews and send invitations."""
    tp_cfg = config.get("trustpilot", {})
    if not tp_cfg.get("enabled", False):
        return
    api_key = tp_cfg["api_key"]
    api_secret = tp_cfg["api_secret"]
    business_unit_id = tp_cfg["business_unit_id"]
    auto_reply = tp_cfg.get("auto_reply", {})
    invitation_cfg = tp_cfg.get("review_invitation", {})

    access_token = trustpilot_get_access_token(api_key, api_secret)
    if not access_token:
        return

    # Auto‑reply
    reviews = trustpilot_get_reviews(access_token, business_unit_id)
    replied_ids = set(state.get("trustpilot_replied_review_ids", []))
    reply_template = auto_reply.get("template", "Thanks for your review!")
    for review in reviews:
        review_id = review.get("id")
        if review_id in replied_ids:
            continue
        reviewer_name = review.get("consumer", {}).get("displayName", "there")
        message = reply_template.replace("{reviewer_name}", reviewer_name)
        # Optional LLM enhancement (simplified: if config has llm, use it)
        llm_cfg = auto_reply.get("llm")
        if llm_cfg and llm_cfg.get("api_key"):
            # We'll skip full implementation to avoid external dependency but it's possible
            pass

        if trustpilot_reply_to_review(access_token, review_id, message):
            _post(f"Trustpilot reply to {reviewer_name} (ID {review_id})", "info")
            replied_ids.add(review_id)
            state["trustpilot_replied_review_ids"] = list(replied_ids)[-500:]
            time.sleep(1)

        # Competitor flags
        competitor_cfg = config.get("competitor_flags", {})
        if competitor_cfg.get("enabled"):
            review_text = review.get("text", "")
            matched = flag_competitor_mentions(review_text, competitor_cfg.get("keywords", []))
            if matched:
                _post(f"Competitor mention in Trustpilot review {review_id}: '{matched}'", "warning", {
                    "review_id": review_id,
                    "competitor": matched
                })

    # Send invitations
    if invitation_cfg.get("enabled"):
        source_file = invitation_cfg.get("source_file", "")
        if os.path.exists(source_file):
            with open(source_file, "r") as f:
                customers = json.load(f)
            template_id = invitation_cfg.get("template_id")
            invited_emails = set(state.get("trustpilot_invited_emails", []))
            for cust in customers:
                email = cust.get("email")
                if email in invited_emails:
                    continue
                name = cust.get("name", "")
                if trustpilot_send_invitation(access_token, business_unit_id, template_id, email, name):
                    _post(f"Invitation sent to {email}", "info")
                    invited_emails.add(email)
                    state["trustpilot_invited_emails"] = list(invited_emails)[-1000:]
                    time.sleep(1)

def process_g2(config: dict, state: dict):
    """Handle G2: reply to new reviews and send invitations."""
    g2_cfg = config.get("g2", {})
    if not g2_cfg.get("enabled", False):
        return
    api_token = g2_cfg["api_token"]
    product_id = g2_cfg["product_id"]
    auto_reply = g2_cfg.get("auto_reply", {})
    invitation_cfg = g2_cfg.get("review_invitation", {})

    # Auto‑reply
    reviews = g2_get_reviews(api_token, product_id)
    replied_ids = set(state.get("g2_replied_review_ids", []))
    reply_template = auto_reply.get("template", "Thanks for your review!")
    for review in reviews:
        review_id = review.get("id")
        if review_id in replied_ids:
            continue
        reviewer_name = review.get("attributes", {}).get("user_display_name", "there")
        message = reply_template.replace("{reviewer_name}", reviewer_name)
        if g2_reply_to_review(api_token, review_id, message):
            _post(f"G2 reply to {reviewer_name} (ID {review_id})", "info")
            replied_ids.add(review_id)
            state["g2_replied_review_ids"] = list(replied_ids)[-500:]
            time.sleep(1)

        # Competitor flags
        competitor_cfg = config.get("competitor_flags", {})
        if competitor_cfg.get("enabled"):
            review_body = review.get("attributes", {}).get("body", "")
            matched = flag_competitor_mentions(review_body, competitor_cfg.get("keywords", []))
            if matched:
                _post(f"Competitor mention in G2 review {review_id}: '{matched}'", "warning", {
                    "review_id": review_id,
                    "competitor": matched
                })

    # Send invitations
    if invitation_cfg.get("enabled"):
        source_file = invitation_cfg.get("source_file", "")
        if os.path.exists(source_file):
            with open(source_file, "r") as f:
                customers = json.load(f)
            invited_emails = set(state.get("g2_invited_emails", []))
            for cust in customers:
                email = cust.get("email")
                if email in invited_emails:
                    continue
                name = cust.get("name", "")
                if g2_send_invitation(api_token, product_id, email, name):
                    _post(f"G2 invitation sent to {email}", "info")
                    invited_emails.add(email)
                    state["g2_invited_emails"] = list(invited_emails)[-1000:]
                    time.sleep(1)

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Trustpilot / G2 Review Management Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "trustpilot_g2_state.json")
        state = load_state(state_file)

        process_trustpilot(config, state)
        process_g2(config, state)

        save_state(state_file, state)

        poll_min = int(config.get("poll_interval_minutes", 60))
        _heartbeat()
        time.sleep(poll_min * 60)

if __name__ == "__main__":
    main()
