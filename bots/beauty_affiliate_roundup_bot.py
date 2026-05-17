#!/usr/bin/env python3
"""
beauty_affiliate_roundup_bot.py — Beauty Affiliate Round‑up Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Auto‑publishes “Top 10”‑style blog posts to WordPress and
Pinterest daily, using an LLM to generate content and
affiliate links from a catalog.  Attachable to the Nazgul
BotController (http://localhost:8765).

Requirements
────────────
    pip install requests openai

Configuration
─────────────
Place `beauty_affiliate_roundup_config.json` in the same directory:

{
  "llm": {
    "provider": "openai",
    "api_key": "sk-...",
    "model": "gpt-4o-mini",
    "temperature": 0.8,
    "max_tokens": 2000
  },
  "wordpress": {
    "url": "https://your-site.com",
    "username": "your_wp_username",
    "application_password": "your_app_password"
  },
  "pinterest": {
    "access_token": "YOUR_PINTEREST_ACCESS_TOKEN",
    "board_id": "123456789012345678"
  },
  "catalog_file": "beauty_affiliate_products.json",
  "themes": [
    "spring lipsticks",
    "summer foundations",
    "fall skincare essentials",
    "winter hair care",
    "budget-friendly mascaras",
    "clean beauty favourites"
  ],
  "default_image_url": "https://via.placeholder.com/800x400.png?text=Top+Picks",
  "poll_interval_hours": 24,
  "state_file": "beauty_affiliate_roundup_state.json",
  "heartbeat_interval": 30
}

Catalog file (`beauty_affiliate_products.json`) – array of objects:
[
  {
    "name": "Rare Beauty Soft Pinch Liquid Blush",
    "affiliate_link": "https://www.sephora.com/product/rare-beauty-soft-pinch-liquid-blush-P123456?ref=your_tag",
    "image_url": "https://example.com/image.jpg"
  },
  ...
]
"""

import json
import os
import random
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Any

import requests
import openai

# ── Hub connection ───────────────────────────────────────────────
HUB = "http://localhost:8765"
BOT_ID = "beauty_affiliate_roundup_bot"
BOT_NAME = "Beauty Affiliate Round‑up"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "beauty_affiliate_roundup_config.json"
CONFIG_PATH = Path(__file__).with_name(CONFIG_NAME)
if not CONFIG_PATH.exists():
    CONFIG_PATH = Path(CONFIG_NAME)

# ── Hub helpers ──────────────────────────────────────────────────
def _post(summary: str, level: str = "info", payload: dict = None) -> None:
    try:
        requests.post(
            f"{HUB}/ingest",
            json={
                "bot_id": BOT_ID,
                "bot_name": BOT_NAME,
                "summary": summary,
                "level": level,
                "payload": payload or {},
            },
            timeout=5,
        )
    except Exception:
        pass

def _heartbeat() -> None:
    global _last_hb
    if time.time() - _last_hb < HEARTBEAT_INTERVAL:
        return
    try:
        requests.post(
            f"{HUB}/heartbeat/{BOT_ID}",
            json={"bot_name": BOT_NAME, "status": "online"},
            timeout=3,
        )
    except Exception:
        pass
    _last_hb = time.time()

# ── State persistence ────────────────────────────────────────────
def load_state(state_file: str) -> dict:
    try:
        with open(state_file, "r") as f:
            return json.load(f)
    except Exception:
        return {"last_run": None, "published_posts": []}

def save_state(state_file: str, state: dict) -> None:
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2, default=str)

# ── LLM generation ───────────────────────────────────────────────
def generate_roundup_post(
    theme: str,
    products: List[dict],
    llm_config: dict,
) -> Optional[dict]:
    """
    Ask OpenAI to produce a blog post (title, HTML content) that features
    the given products with affiliate links.  Returns a dict with keys
    'title' and 'content' (HTML).  Returns None on failure.
    """
    if not llm_config.get("api_key"):
        _post("OpenAI API key missing", "error")
        return None

    # Build a product list for the prompt
    product_lines = []
    for p in products:
        name = p["name"]
        link = p.get("affiliate_link", "")
        product_lines.append(f"- {name} ({link})")
    product_list = "\n".join(product_lines)

    prompt = f"""
You are a beauty editor for a popular blog. Write a "Top 10" blog post about {theme}.
Use the following products and their affiliate links naturally within the content.
Each product should be mentioned in a list with a short description and a link.

Products:
{product_list}

Format your response as a JSON object with two fields:
  "title": a catchy, SEO-friendly title for the blog post
  "content": the full HTML content of the post (use <p>, <ul>, <li>, <a> tags appropriately).
Only return the JSON object, no other text.
"""

    # Call OpenAI
    openai.api_key = llm_config["api_key"]
    try:
        response = openai.ChatCompletion.create(
            model=llm_config.get("model", "gpt-4o-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=float(llm_config.get("temperature", 0.8)),
            max_tokens=int(llm_config.get("max_tokens", 2000)),
        )
        result_text = response.choices[0].message.content.strip()
        # Clean up possible markdown fences
        if result_text.startswith("```"):
            lines = result_text.splitlines()
            lines = [l for l in lines if not l.startswith("```")]
            result_text = "\n".join(lines)
        post_data = json.loads(result_text)
        return post_data
    except Exception as e:
        _post(f"LLM generation failed: {e}", "error")
        return None

# ── WordPress API ────────────────────────────────────────────────
def publish_wp_post(
    wp_url: str,
    username: str,
    app_password: str,
    title: str,
    content: str,
    featured_media_id: Optional[int] = None,
) -> Optional[int]:
    """Publish a new WordPress post and return its ID."""
    api_url = f"{wp_url.rstrip('/')}/wp-json/wp/v2/posts"
    auth = (username, app_password)
    post_data = {
        "title": title,
        "content": content,
        "status": "publish",
        "format": "standard",
    }
    if featured_media_id:
        post_data["featured_media"] = featured_media_id

    try:
        resp = requests.post(api_url, json=post_data, auth=auth, timeout=20)
        if resp.status_code == 201:
            return resp.json()["id"]
        else:
            _post(f"WordPress publish error: {resp.status_code} {resp.text[:300]}", "error")
            return None
    except Exception as e:
        _post(f"WordPress request failed: {e}", "error")
        return None

def upload_image_to_wp(wp_url: str, username: str, app_password: str,
                       image_url: str) -> Optional[int]:
    """Upload an image to WordPress and return the media ID."""
    api_url = f"{wp_url.rstrip('/')}/wp-json/wp/v2/media"
    auth = (username, app_password)
    # Download the image first
    try:
        img_resp = requests.get(image_url, timeout=15)
        if img_resp.status_code != 200:
            _post(f"Failed to download image {image_url}", "error")
            return None
        content_type = img_resp.headers.get("Content-Type", "image/jpeg")
        file_name = image_url.split("/")[-1] or "image.jpg"
        resp = requests.post(
            api_url,
            auth=auth,
            headers={
                "Content-Disposition": f'attachment; filename="{file_name}"',
                "Content-Type": content_type,
            },
            data=img_resp.content,
            timeout=20,
        )
        if resp.status_code == 201:
            return resp.json()["id"]
        else:
            _post(f"WordPress media upload error: {resp.status_code} {resp.text[:200]}", "error")
            return None
    except Exception as e:
        _post(f"WordPress media upload failed: {e}", "error")
        return None

# ── Pinterest API ────────────────────────────────────────────────
PINTEREST_API = "https://api.pinterest.com/v5"

def create_pinterest_pin(
    access_token: str,
    board_id: str,
    title: str,
    description: str,
    link: str,
    image_url: str,
) -> bool:
    """Create a pin. Returns True on success."""
    url = f"{PINTEREST_API}/pins"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    body = {
        "board_id": board_id,
        "title": title,
        "description": description,
        "link": link,
        "media_source": {
            "source_type": "image_url",
            "url": image_url,
        },
    }
    try:
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            return True
        else:
            _post(f"Pinterest pin error: {resp.status_code} {resp.text[:300]}", "error")
            return False
    except Exception as e:
        _post(f"Pinterest request failed: {e}", "error")
        return False

# ── Main routine ─────────────────────────────────────────────────
def run_roundup(config: dict, state: dict):
    """Generate and publish the roundup."""
    # Check if it's time to run
    poll_hours = float(config.get("poll_interval_hours", 24))
    last_run_str = state.get("last_run")
    if last_run_str:
        last_run = datetime.fromisoformat(last_run_str)
        if datetime.now(timezone.utc) - last_run < timedelta(hours=poll_hours):
            _post("Not yet time for next roundup", "info")
            return

    # Pick a theme
    themes = config.get("themes", [])
    if not themes:
        _post("No themes configured", "error")
        return
    theme = random.choice(themes)
    _post(f"Generating roundup for theme: {theme}", "info")

    # Load product catalog
    catalog_file = config.get("catalog_file", "beauty_affiliate_products.json")
    try:
        with open(catalog_file, "r") as f:
            products = json.load(f)
        if not isinstance(products, list):
            _post("Catalog file is not a list", "error")
            return
    except Exception as e:
        _post(f"Catalog load error: {e}", "error")
        return

    # Select up to 10 random products (or use all if <=10)
    if len(products) > 10:
        selected = random.sample(products, 10)
    else:
        selected = products

    # Generate blog post content via LLM
    llm_config = config.get("llm", {})
    post_data = generate_roundup_post(theme, selected, llm_config)
    if not post_data:
        _post("Failed to generate post content", "error")
        return

    post_title = post_data.get("title", theme.title())
    post_content = post_data.get("content", "")

    # Publish to WordPress
    wp_config = config.get("wordpress", {})
    wp_url = wp_config.get("url")
    wp_user = wp_config.get("username")
    wp_pass = wp_config.get("application_password")
    if not all([wp_url, wp_user, wp_pass]):
        _post("WordPress credentials not fully configured; skipping blog publish", "warning")
        post_url = None
    else:
        # Upload a featured image if a default image URL is provided
        default_image = config.get("default_image_url")
        media_id = None
        if default_image:
            media_id = upload_image_to_wp(wp_url, wp_user, wp_pass, default_image)
        post_id = publish_wp_post(wp_url, wp_user, wp_pass, post_title, post_content, media_id)
        if post_id:
            post_url = f"{wp_url.rstrip('/')}/{post_id}"
            _post(f"WordPress post published: {post_url}", "info", {"post_id": post_id})
        else:
            _post("WordPress post failed", "error")
            post_url = None

    # Publish to Pinterest (one main pin)
    pinterest_config = config.get("pinterest", {})
    p_access_token = pinterest_config.get("access_token")
    p_board_id = pinterest_config.get("board_id")
    if not all([p_access_token, p_board_id]):
        _post("Pinterest credentials missing; skipping pin creation", "warning")
    else:
        pin_title = post_title
        pin_description = f"Check out our top picks for {theme}. #beauty #affiliate"
        pin_link = post_url if post_url else "https://your-site.com"  # fallback
        pin_image = config.get("default_image_url", "https://via.placeholder.com/800x400.png?text=Top+Picks")
        # Use the first product's image if available
        if selected and selected[0].get("image_url"):
            pin_image = selected[0]["image_url"]
        success = create_pinterest_pin(
            p_access_token, p_board_id, pin_title, pin_description, pin_link, pin_image
        )
        if success:
            _post(f"Pinterest pin created for {pin_title}", "info")
            # Optionally create additional pins for each product (can be added later)
        else:
            _post("Failed to create Pinterest pin", "error")

    # Record this run
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    state.setdefault("published_posts", []).append({"theme": theme, "title": post_title})

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Beauty Affiliate Round‑up Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Config error: {e}", "error")
            time.sleep(60)
            continue

        state_file = config.get("state_file", "beauty_affiliate_roundup_state.json")
        state = load_state(state_file)

        run_roundup(config, state)

        save_state(state_file, state)
        _heartbeat()
        # Check every 60 seconds to see if it's time to run (the run_roundup function handles the timing)
        time.sleep(60)

if __name__ == "__main__":
    main()
