#!/usr/bin/env python3
"""
trending_topic_bot.py — Trending Topic Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scans Google Trends, TikTok, and YouTube for
viral topics and reports them to the Nazgul
BotController.

Attachable to the BotController dashboard
(http://localhost:8765).

Requirements
────────────
    pip install requests pytrends google-api-python-client TikTokApi

Configuration
─────────────
Place `trending_config.json` in the same directory:

{
  "providers": {
    "google_trends": {
      "enabled": true,
      "keywords": ["AI", "blockchain", "crypto"],
      "geo": "US",
      "timeframe": "now 7-d"
    },
    "tiktok": {
      "enabled": true,
      "keyword": "viral",
      "count": 10,
      "cookies": {
        "tt_webid": "your_web_id",
        "tt_webid_v2": "your_webid_v2",
        "tt_csrf_token": "your_csrf_token",
        "sessionid": "your_session_id"
      }
    },
    "youtube": {
      "enabled": true,
      "api_key": "YOUR_YOUTUBE_DATA_API_KEY",
      "region_code": "US",
      "max_results": 10,
      "category_id": null
    }
  },
  "poll_interval_minutes": 60,
  "state_file": "trending_state.json"
}
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Hub connection ───────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "trending_topic_bot"
BOT_NAME = "Trending Topic Bot"

HEARTBEAT_INTERVAL = 30
_last_hb = 0.0

CONFIG_NAME = "trending_config.json"
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

# ── Google Trends ────────────────────────────────────────────────
def fetch_google_trends(config: dict) -> list:
    try:
        from pytrends.request import TrendReq
    except ImportError:
        _post("pytrends not installed. pip install pytrends", "error")
        return []

    cfg = config.get("providers", {}).get("google_trends", {})
    if not cfg.get("enabled", True):
        return []

    keywords = cfg.get("keywords", [])
    geo = cfg.get("geo", "US")
    timeframe = cfg.get("timeframe", "now 7-d")

    if not keywords:
        return []

    try:
        pytrends = TrendReq(hl="en-US", tz=360)
        pytrends.build_payload(keywords, cat=0, timeframe=timeframe, geo=geo, gprop="")
        interest_df = pytrends.interest_over_time()
        if interest_df.empty:
            return []

        # Get the latest row (most recent date with data)
        latest = interest_df.iloc[-1]
        results = []
        for kw in keywords:
            if kw in latest:
                results.append({
                    "source": "google_trends",
                    "keyword": kw,
                    "interest": int(latest[kw]),
                    "geo": geo,
                    "timeframe": timeframe,
                    "date": str(latest.name.date()) if hasattr(latest, "name") else ""
                })
        return results
    except Exception as e:
        _post(f"Google Trends error: {e}", "warning")
        return []

# ── TikTok ───────────────────────────────────────────────────────
def fetch_tiktok_trends(config: dict) -> list:
    cfg = config.get("providers", {}).get("tiktok", {})
    if not cfg.get("enabled", True):
        return []

    keyword = cfg.get("keyword", "viral")
    count = int(cfg.get("count", 10))
    cookies = cfg.get("cookies", {})

    try:
        from TikTokApi import TikTokApi
    except ImportError:
        _post("TikTokApi not installed. pip install TikTokApi", "error")
        return []

    try:
        api = TikTokApi()
        # Use provided cookies for authentication (works for some searches)
        # The API may require a ms_token; we pass cookies as dict
        ms_token = cookies.get("ms_token")
        if not ms_token:
            ms_token = None  # might still work anonymously
        # Create a session with cookies
        api.sessions.selenium_session(ms_tokens=[ms_token]) if ms_token else None

        # Search for trending content
        trending = api.search_type(
            keyword,
            count=count,
            type=0,  # 0 = video
        )
        results = []
        for t in trending:
            stats = t.get("stats", {})
            video = t.get("video", {})
            results.append({
                "source": "tiktok",
                "id": t.get("id", ""),
                "desc": t.get("desc", ""),
                "play_count": stats.get("playCount", 0),
                "like_count": stats.get("diggCount", 0),
                "share_count": stats.get("shareCount", 0),
                "author": t.get("author", {}).get("uniqueId", "")
            })
        return results
    except Exception as e:
        _post(f"TikTok error: {e}", "warning")
        return []

# ── YouTube ──────────────────────────────────────────────────────
def fetch_youtube_trends(config: dict) -> list:
    cfg = config.get("providers", {}).get("youtube", {})
    if not cfg.get("enabled", True):
        return []
    api_key = cfg.get("api_key")
    if not api_key:
        _post("YouTube API key not configured", "error")
        return []

    try:
        from googleapiclient.discovery import build
    except ImportError:
        _post("google-api-python-client not installed", "error")
        return []

    region_code = cfg.get("region_code", "US")
    max_results = int(cfg.get("max_results", 10))
    category_id = cfg.get("category_id")

    try:
        youtube = build("youtube", "v3", developerKey=api_key)
        request_params = {
            "part": "snippet",
            "chart": "mostPopular",
            "regionCode": region_code,
            "maxResults": max_results
        }
        if category_id:
            request_params["videoCategoryId"] = category_id

        request = youtube.videos().list(**request_params)
        response = request.execute()

        results = []
        for item in response.get("items", []):
            snippet = item.get("snippet", {})
            results.append({
                "source": "youtube",
                "video_id": item.get("id"),
                "title": snippet.get("title"),
                "channel": snippet.get("channelTitle"),
                "published_at": snippet.get("publishedAt")
            })
        return results
    except Exception as e:
        _post(f"YouTube API error: {e}", "warning")
        return []

# ── Main loop ────────────────────────────────────────────────────
def main():
    _post("Trending Topic Bot online")
    while True:
        try:
            with open(CONFIG_PATH, "r") as f:
                config = json.load(f)
        except Exception as e:
            _post(f"Failed to load config: {e}", "error")
            time.sleep(60)
            continue

        poll_minutes = int(config.get("poll_interval_minutes", 60))

        # Google Trends
        google_data = fetch_google_trends(config)
        for item in google_data:
            _post(
                f"[Google Trends] {item['keyword']} interest: {item['interest']}",
                "info",
                item
            )
        _post(f"Google Trends: {len(google_data)} topics", "info")

        # TikTok
        tiktok_data = fetch_tiktok_trends(config)
        for item in tiktok_data:
            _post(
                f"[TikTok] {item['desc'][:100]} (likes: {item['like_count']})",
                "info",
                item
            )
        _post(f"TikTok: {len(tiktok_data)} videos", "info")

        # YouTube
        youtube_data = fetch_youtube_trends(config)
        for item in youtube_data:
            _post(
                f"[YouTube] {item['title'][:100]} by {item['channel']}",
                "info",
                item
            )
        _post(f"YouTube: {len(youtube_data)} trending videos", "info")

        _heartbeat()
        time.sleep(poll_minutes * 60)

if __name__ == "__main__":
    main()
