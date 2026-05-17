#!/usr/bin/env python3
"""
producthunt_engagement_bot.py — Product Hunt Drops & Engagement Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Schedules product launches via the Product Hunt API v2,
auto‑replies to reviews/comments on your product, and
engages in competitor discussions by posting constructive
comments that link back to your product.

Attachable to the Nazgul BotController (http://localhost:8765).

Requirements
────────────
    pip install requests

Configuration
─────────────
Place `producthunt_engagement_config.json` in the same directory:

{
  "product_hunt": {
    "developer_token": "YOUR_DEVELOPER_TOKEN",
    "product_id": 12345                         // your existing product ID (optional)
  },
  "launch": {
    "enabled": true,
    "name": "My Awesome Tool",
    "tagline": "A short, catchy tagline.",
    "description": "A longer description of the product.",
    "website_url": "https://example.com",
    "topics": ["Productivity", "Developer Tools"],
    "scheduled_at": "2025-02-01T12:00:00Z"      // ISO 8601; leave empty for immediate launch
  },
  "auto_reply": {
    "enabled": true,
    "reply_template": "Thanks for your feedback, {username}! We're constantly improving – check out our roadmap: https://example.com/roadmap",
    "llm": null                                  // optional OpenAI config
  },
  "competitor_engagement": {
    "enabled": true,
    "keywords": ["competitor1", "competitor2"],
    "comment_template": "Interesting! I've built a similar solution at {link} – would love to hear your thoughts.",
    "link": "https://example.com",
    "max_posts_to_engage": 3
  },
  "state_file": "producthunt_engagement_state.json",
  "heartbeat_interval": 30,
  "poll_interval_seconds": 300
}
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "producthunt_engagement_bot"
BOT_NAME = "Product Hunt Drops & Engagement"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "producthunt_engagement_config.json"
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
            "launched_post_id": None,
            "replied_comment_ids": [],
            "engaged_post_ids": []
        }

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── Product Hunt API v2 (GraphQL) ───────────────────────────────
PH_GRAPHQL_URL = "https://api.producthunt.com/v2/api/graphql"

def _graphql_request(token: str, query: str, variables: dict = None) -> Optional[dict]:
    """Send a GraphQL request to Product Hunt."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    payload = {"query": query, "variables": variables or {}}
    try:
        resp = requests.post(PH_GRAPHQL_URL, json=payload, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            if "errors" in data:
                _post(f"GraphQL errors: {data['errors']}", "error")
                return None
            return data.get("data")
        else:
            _post(f"PH API HTTP {resp.status_code}: {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"PH GraphQL request error: {e}", "error")
        return None

# ── Launch product ──────────────────────────────────────────────
def create_product(token: str, name: str, tagline: str, description: str,
                   website: str, topics: List[str] = None) -> Optional[int]:
    """Create a new product. Returns product ID."""
    mutation = """
    mutation CreateProduct($name: String!, $tagline: String!, $description: String!, $websiteUrl: String!, $topics: [String!]) {
      createProduct(input: {
        name: $name,
        tagline: $tagline,
        description: $description,
        websiteUrl: $websiteUrl,
        topics: $topics
      }) {
        product {
          id
        }
      }
    }
    """
    variables = {
        "name": name,
        "tagline": tagline,
        "description": description,
        "websiteUrl": website,
        "topics": topics or []
    }
    data = _graphql_request(token, mutation, variables)
    if data and "createProduct" in data:
        return int(data["createProduct"]["product"]["id"])
    return None

def create_post(token: str, product_id: int, scheduled_at: Optional[str] = None) -> Optional[int]:
    """Create a post for a product. Returns post ID."""
    mutation = """
    mutation CreatePost($productId: ID!, $scheduledAt: DateTime) {
      createPost(input: {
        productId: $productId,
        scheduledAt: $scheduledAt
      }) {
        post {
          id
        }
      }
    }
    """
    variables = {
        "productId": str(product_id),
        "scheduledAt": scheduled_at or None
    }
    data = _graphql_request(token, mutation, variables)
    if data and "createPost" in data:
        return int(data["createPost"]["post"]["id"])
    return None

def schedule_launch(config: dict, state: dict):
    """If a launch is enabled and not yet done, create the product (if needed) and schedule the post."""
    launch = config.get("launch", {})
    if not launch.get("enabled") or state.get("launched_post_id"):
        return
    token = config["product_hunt"]["developer_token"]
    product_id = config["product_hunt"].get("product_id")
    if not product_id:
        # Need to create product first
        product_id = create_product(
            token,
            launch["name"],
            launch["tagline"],
            launch["description"],
            launch["website_url"],
            launch.get("topics", [])
        )
        if not product_id:
            _post("Failed to create product on Product Hunt", "error")
            return
        # Store product_id in config? We'll store in state for next runs
        state["product_id"] = product_id

    # Create post (scheduled or immediate)
    scheduled_at = launch.get("scheduled_at")
    post_id = create_post(token, product_id, scheduled_at)
    if post_id:
        _post(f"Product Hunt post created (ID {post_id}) – scheduled for {scheduled_at or 'immediate'}", "info")
        state["launched_post_id"] = post_id
    else:
        _post("Failed to create post", "error")

# ── Auto‑reply to comments ──────────────────────────────────────
def fetch_comments(token: str, post_id: int, limit: int = 20) -> List[dict]:
    """Fetch recent comments on a post."""
    query = """
    query PostComments($postId: ID!, $limit: Int) {
      post(id: $postId) {
        comments(first: $limit, order: CREATED_AT_DESC) {
          edges {
            node {
              id
              body
              user {
                username
              }
            }
          }
        }
      }
    }
    """
    variables = {"postId": str(post_id), "limit": limit}
    data = _graphql_request(token, query, variables)
    if data and "post" in data and data["post"]:
        edges = data["post"]["comments"]["edges"]
        return [edge["node"] for edge in edges]
    return []

def reply_to_comment(token: str, comment_id: str, body: str) -> bool:
    """Reply to a specific comment."""
    mutation = """
    mutation CreateReply($commentId: ID!, $body: String!) {
      createComment(input: {
        commentableId: $commentId,
        body: $body
      }) {
        comment {
          id
        }
      }
    }
    """
    variables = {"commentId": comment_id, "body": body}
    data = _graphql_request(token, mutation, variables)
    return data is not None and "createComment" in data

def process_auto_replies(config: dict, state: dict):
    """Reply to new comments on the launched post."""
    auto_reply_cfg = config.get("auto_reply", {})
    if not auto_reply_cfg.get("enabled"):
        return
    post_id = state.get("launched_post_id")
    if not post_id:
        return
    token = config["product_hunt"]["developer_token"]
    comments = fetch_comments(token, post_id)
    if not comments:
        return
    replied_ids = set(state.get("replied_comment_ids", []))
    template = auto_reply_cfg.get("reply_template", "")
    if not template:
        return
    for comment in comments:
        if comment["id"] in replied_ids:
            continue
        username = comment.get("user", {}).get("username", "there")
        body = template.replace("{username}", username)
        # Optional LLM generation can be added here using config["auto_reply"]["llm"]
        if reply_to_comment(token, comment["id"], body):
            _post(f"Replied to comment {comment['id']} by {username}", "info")
            replied_ids.add(comment["id"])
            state["replied_comment_ids"] = list(replied_ids)[-500:]
            time.sleep(1)  # rate limit
        else:
            _post(f"Failed to reply to comment {comment['id']}", "error")

# ── Competitor discussion engagement ─────────────────────────────
def search_posts(token: str, query: str, limit: int = 5) -> List[dict]:
    """Search for posts by keyword."""
    graphql_query = """
    query SearchPosts($query: String!, $first: Int) {
      posts(first: $first, search: $query) {
        edges {
          node {
            id
            name
            tagline
            commentsCount
          }
        }
      }
    }
    """
    variables = {"query": query, "first": limit}
    data = _graphql_request(token, graphql_query, variables)
    if data and "posts" in data:
        return [edge["node"] for edge in data["posts"]["edges"]]
    return []

def engage_with_competitor_discussions(config: dict, state: dict):
    """Find discussions about competitors and leave a helpful comment."""
    comp_cfg = config.get("competitor_engagement", {})
    if not comp_cfg.get("enabled"):
        return
    token = config["product_hunt"]["developer_token"]
    keywords = comp_cfg.get("keywords", [])
    max_engage = int(comp_cfg.get("max_posts_to_engage", 3))
    comment_template = comp_cfg.get("comment_template", "")
    link = comp_cfg.get("link", "")
    if not keywords or not comment_template:
        return
    engaged_ids = set(state.get("engaged_post_ids", []))
    new_engage = 0
    for kw in keywords:
        posts = search_posts(token, kw)
        for post in posts:
            if new_engage >= max_engage:
                return
            if post["id"] in engaged_ids:
                continue
            body = comment_template.replace("{link}", link)
            # To leave a comment, we need to comment on the post itself, not a comment.
            # The mutation `createComment` with `commentableId` of the post works.
            if reply_to_comment(token, post["id"], body):  # same mutation works for posts
                _post(f"Engaged with post {post['id']} ({post['name']})", "info")
                engaged_ids.add(post["id"])
                state["engaged_post_ids"] = list(engaged_ids)[-500:]
                new_engage += 1
                time.sleep(1)
            else:
                _post(f"Failed to comment on post {post['id']}", "error")

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Product Hunt Drops & Engagement Bot online")
    _post("Note: auto‑thanking upvoters is not supported by the PH API, but replies to comments will thank users personally.", "info")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        token = config.get("product_hunt", {}).get("developer_token")
        if not token:
            _post("Product Hunt developer token missing", "error")
            time.sleep(300)
            continue

        state_file = config.get("state_file", "producthunt_engagement_state.json")
        state = load_state(state_file)

        # 1. Schedule launch (if not already done)
        schedule_launch(config, state)

        # 2. Auto‑reply to comments
        process_auto_replies(config, state)

        # 3. Engage with competitor discussions
        engage_with_competitor_discussions(config, state)

        save_state(state_file, state)

        poll_sec = int(config.get("poll_interval_seconds", 300))
        _heartbeat()
        time.sleep(poll_sec)

if __name__ == "__main__":
    main()
