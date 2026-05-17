#!/usr/bin/env python3
"""
maternity_review_aggregator_bot.py — Maternity Wear Try‑On Review Bot
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Scrapes YouTube (and optionally TikTok) for genuine maternity clothing
reviews, then compiles them into a shoppable HTML blog post with affiliate
links to every brand mentioned.

Requirements:
    pip install requests beautifulsoup4

Optional (for TikTok):
    pip install TikTokApi playwright
    python -m playwright install

Configuration:
    A file named `maternity_review_config.json` is created on first run.
    You MUST add your YouTube Data API v3 key (free from Google Cloud Console).
    Add your Awin / ShareASale publisher IDs to earn commissions.
"""

import json
import os
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ── Hub connection ──────────────────────────────────────────────────────────
HUB      = "http://localhost:8765"
BOT_ID   = "maternity_review_aggregator"
BOT_NAME = "Maternity Wear Try‑On Review Bot"

# ── Config file ─────────────────────────────────────────────────────────────
CFG_FILE = Path(__file__).with_name("maternity_review_config.json")
STATE_FILE = Path(__file__).with_name("maternity_review_state.json")
BLOG_DIR   = Path(__file__).with_name("maternity_blog_posts")

DEFAULT_CONFIG = {
    "youtube_api_key": "",                     # ★ REQUIRED: get from console.cloud.google.com
    "tiktok_enabled": False,                   # set true after installing TikTokApi + playwright
    "tiktok_ms_token": "",                     # TikTok session token (see README comment)
    "awin_publisher_id": "",                   # your Awin publisher ID (e.g. "12345")
    "shareasale_user_id": "",                  # your ShareASale user ID
    "generic_ref_param": "utm_source",         # fallback referral param name
    "generic_ref_value": "",                   # fallback referral value (e.g. "myblog")
    "search_queries": [
        "maternity clothing try on haul",
        "maternity wear review honest",
        "pregnancy outfits try on",
        "maternity dress review",
        "bump friendly clothing haul"
    ],
    "max_videos_per_query": 5,
    "scan_interval_hours": 12
}

# ── Maternity brand → affiliate mapping ────────────────────────────────────
# Format: canonical_name → { domain, affiliate_network, merchant_id, direct_ref, commission }
MATERNITY_BRANDS = {
    "seraphine": {
        "domain": "seraphine.com",
        "network": "partnerize",              # also on Awin
        "awin_merchant_id": "",               # look up in your Awin account
        "direct_ref": "?ref=blog",
        "commission": "5-10%"
    },
    "hatch": {
        "domain": "hatchcollection.com",
        "network": "shareasale",
        "shareasale_merchant_id": "",
        "direct_ref": "?aff=blog",
        "commission": "8%"
    },
    "pinkblush": {
        "domain": "pinkblushmaternity.com",
        "network": "shareasale",
        "shareasale_merchant_id": "",
        "direct_ref": "",
        "commission": "4%"
    },
    "ingrid and isabel": {
        "domain": "ingridandisabel.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "7.25%"
    },
    "beyond nine": {
        "domain": "beyondnine.co.uk",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "up to 10%"
    },
    "belly bandit": {
        "domain": "bellybandit.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "10%"
    },
    "cake maternity": {
        "domain": "cakematernity.com",
        "network": "shareasale",
        "shareasale_merchant_id": "",
        "direct_ref": "",
        "commission": "10%"
    },
    "soon maternity": {
        "domain": "soonmaternity.com",
        "network": "direct",
        "direct_ref": "",
        "commission": "5-10%"
    },
    "tiffany rose": {
        "domain": "tiffanyrose.com",
        "network": "affiliate_future",
        "direct_ref": "",
        "commission": "attractive"
    },
    "asos": {
        "domain": "asos.com",
        "network": "awin",
        "awin_merchant_id": "10943",           # ASOS US on Awin
        "direct_ref": "",
        "commission": "varies"
    },
    "hm": {
        "domain": "www2.hm.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "varies"
    },
    "zara": {
        "domain": "zara.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "varies"
    },
    "h&m": {
        "domain": "www2.hm.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "varies"
    },
    "gap": {
        "domain": "gap.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "varies"
    },
    "old navy": {
        "domain": "oldnavy.gap.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "varies"
    },
    "nordstrom": {
        "domain": "nordstrom.com",
        "network": "linkshare",
        "direct_ref": "",
        "commission": "varies"
    },
    "amazon": {
        "domain": "amazon.com",
        "network": "amazon_associates",
        "direct_ref": "?tag=blog-20",
        "commission": "varies"
    },
    "target": {
        "domain": "target.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "varies"
    },
    "free people": {
        "domain": "freepeople.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "varies"
    },
    "anthropologie": {
        "domain": "anthropologie.com",
        "network": "awin",
        "awin_merchant_id": "",
        "direct_ref": "",
        "commission": "varies"
    },
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

# ── YouTube Data API v3 ────────────────────────────────────────────────────
YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

def search_youtube(query, api_key, max_results=5):
    """
    Search YouTube for videos matching the query.
    Returns list of dicts: { video_id, title, description, channel, published_at, thumbnail }
    """
    if not api_key:
        post_to_hub("YouTube API key not configured — cannot search.", "warning")
        return []

    params = {
        "part": "snippet",
        "q": query,
        "type": "video",
        "maxResults": min(max_results, 50),
        "order": "relevance",
        "relevanceLanguage": "en",
        "key": api_key
    }
    try:
        r = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        videos = []
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            if not vid:
                continue
            snippet = item.get("snippet", {})
            videos.append({
                "video_id": vid,
                "title": snippet.get("title", ""),
                "description": snippet.get("description", "")[:300],
                "channel": snippet.get("channelTitle", ""),
                "published_at": snippet.get("publishedAt", ""),
                "thumbnail": snippet.get("thumbnails", {}).get("medium", {}).get("url", ""),
                "url": f"https://www.youtube.com/watch?v={vid}"
            })
        return videos
    except requests.exceptions.HTTPError as e:
        err_body = ""
        try:
            err_body = r.json()
        except Exception:
            err_body = r.text[:200]
        post_to_hub(f"YouTube API error: {e} — {err_body}", "error")
        return []
    except Exception as e:
        post_to_hub(f"YouTube search failed: {e}", "error")
        return []

def get_video_statistics(video_ids, api_key):
    """Get view count, like count, etc. for a list of video IDs."""
    if not video_ids or not api_key:
        return {}
    params = {
        "part": "statistics",
        "id": ",".join(video_ids),
        "key": api_key
    }
    try:
        r = requests.get(YOUTUBE_VIDEOS_URL, params=params, timeout=10)
        r.raise_for_status()
        data = r.json()
        stats = {}
        for item in data.get("items", []):
            vid = item.get("id")
            if vid:
                stats[vid] = item.get("statistics", {})
        return stats
    except Exception:
        return {}

# ── TikTok search (optional, using TikTokApi) ──────────────────────────────
def search_tiktok(query, ms_token, max_results=5):
    """Attempt TikTok search. Returns [] if TikTokApi not installed or fails."""
    try:
        from TikTokApi import TikTokApi
    except ImportError:
        return []   # not installed

    if not ms_token:
        return []

    try:
        results = []
        # TikTokApi v7+ requires async
        import asyncio

        async def _search():
            out = []
            async with TikTokApi() as api:
                await api.create_sessions(ms_tokens=[ms_token], num_sessions=1,
                                          headless=True, sleep_after=3)
                count = 0
                async for video in api.search.videos(query, count=max_results):
                    out.append({
                        "video_id": video.id,
                        "title": getattr(video, 'title', '') or '',
                        "author": getattr(video.author, 'username', '') if video.author else '',
                        "url": f"https://www.tiktok.com/@{video.author.username}/video/{video.id}"
                               if video.author else f"https://www.tiktok.com/video/{video.id}",
                        "thumbnail": getattr(video, 'cover', '') or '',
                        "play_count": getattr(video, 'play_count', 0) or 0,
                    })
                    count += 1
            return out

        # Run async in a sync context
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're inside an event loop already; use a thread
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as executor:
                    future = executor.submit(asyncio.run, _search())
                    results = future.result(timeout=60)
            else:
                results = asyncio.run(_search())
        except RuntimeError:
            results = asyncio.run(_search())

        return results
    except Exception as e:
        post_to_hub(f"TikTok search failed: {e}", "warning")
        return []

# ── Brand matching ─────────────────────────────────────────────────────────
def match_brands(text):
    """Scan text for mentions of known maternity brands. Returns list of brand canonical names."""
    found = []
    text_lower = text.lower()
    for brand_name in MATERNITY_BRANDS:
        # Check if brand name or domain appears in the text
        domain = MATERNITY_BRANDS[brand_name]["domain"]
        if brand_name in text_lower or domain.replace(".com", "").replace("www.", "") in text_lower:
            found.append(brand_name)
    return list(set(found))

def build_affiliate_link(brand_name, config):
    """Build the best available affiliate link for a brand."""
    brand = MATERNITY_BRANDS.get(brand_name, {})
    if not brand:
        return f"https://{brand_name}.com"

    domain = brand["domain"]
    base_url = f"https://{domain}"

    network = brand.get("network", "direct")

    # Awin
    if network == "awin":
        merchant_id = brand.get("awin_merchant_id", "")
        publisher_id = config.get("awin_publisher_id", "")
        if merchant_id and publisher_id:
            encoded = requests.utils.quote(base_url, safe="")
            return f"https://www.awin1.com/cread.php?awinmid={merchant_id}&awinaffid={publisher_id}&ued={encoded}"

    # ShareASale
    if network == "shareasale":
        merchant_id = brand.get("shareasale_merchant_id", "")
        user_id = config.get("shareasale_user_id", "")
        if merchant_id and user_id:
            return f"https://www.shareasale.com/m-pr.cfm?merchantID={merchant_id}&userID={user_id}"

    # Direct referral param
    direct_ref = brand.get("direct_ref", "")
    if direct_ref:
        return f"{base_url}{direct_ref}"

    # Generic fallback
    generic_param = config.get("generic_ref_param", "utm_source")
    generic_value = config.get("generic_ref_value", "")
    if generic_value:
        sep = "&" if "?" in base_url else "?"
        return f"{base_url}{sep}{generic_param}={generic_value}"

    return base_url

# ── Blog post generator ────────────────────────────────────────────────────
BLOG_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
        max-width: 800px; margin: 0 auto; padding: 20px; background: #fdf8f5; color: #222; }}
  h1 {{ color: #8b5e6b; border-bottom: 3px solid #e8d5d0; padding-bottom: 10px; }}
  h2 {{ color: #6b4e5e; margin-top: 30px; }}
  .video-card {{ background: #fff; border: 1px solid #e8d5d0; border-radius: 8px;
                  padding: 15px; margin: 15px 0; box-shadow: 0 2px 6px rgba(0,0,0,0.05); }}
  .video-card h3 {{ margin: 0 0 6px 0; font-size: 1.05em; }}
  .video-card a {{ color: #8b5e6b; text-decoration: none; }}
  .video-card a:hover {{ text-decoration: underline; }}
  .meta {{ color: #888; font-size: 0.85em; }}
  .iframe-container {{ position: relative; padding-bottom: 56.25%; height: 0; overflow: hidden;
                        max-width: 100%; margin: 10px 0; border-radius: 6px; }}
  .iframe-container iframe {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%;
                               border-radius: 6px; }}
  .brands-section {{ background: #fff5f2; border: 1px solid #f0d5cc; border-radius: 8px;
                     padding: 15px; margin: 20px 0; }}
  .brands-section h2 {{ margin-top: 0; }}
  .brand-item {{ display: inline-block; margin: 5px 10px 5px 0; }}
  .brand-item a {{ display: inline-block; padding: 6px 14px; background: #8b5e6b; color: #fff;
                   border-radius: 4px; text-decoration: none; font-size: 0.9em; }}
  .brand-item a:hover {{ background: #6b4e5e; }}
  .tiktok-link {{ display: inline-block; margin: 3px 8px 3px 0; }}
  .tiktok-link a {{ color: #ff0050; font-weight: bold; }}
  .footer {{ margin-top: 40px; padding-top: 15px; border-top: 1px solid #e8d5d0;
             color: #aaa; font-size: 0.8em; text-align: center; }}
  .commission-note {{ color: #888; font-size: 0.78em; font-style: italic; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p>Generated on {date} — curated from real reviews on YouTube and TikTok.</p>
<p class="commission-note">This post contains affiliate links. If you purchase through these links,
we may earn a commission at no extra cost to you.</p>

{youtube_section}
{tiktok_section}
{brands_section}
{footer}
</body>
</html>"""

def generate_blog_post(youtube_videos, tiktok_videos, matched_brands, config, queries):
    """Generate a complete HTML blog post."""
    title = f"Best Maternity Clothing Reviews — Real Try‑Ons from YouTube & TikTok"

    # YouTube section
    youtube_html = ""
    if youtube_videos:
        youtube_html = "<h2>📺 YouTube Reviews</h2>"
        for v in youtube_videos:
            ytid = v.get("video_id", "")
            embed = ""
            if ytid:
                embed = f"""
                <div class="iframe-container">
                  <iframe src="https://www.youtube.com/embed/{ytid}"
                          frameborder="0" allowfullscreen></iframe>
                </div>"""
            views = v.get("view_count", "")
            views_str = f" · {int(views):,} views" if views else ""
            youtube_html += f"""
            <div class="video-card">
              <h3><a href="{v['url']}" target="_blank">{v['title']}</a></h3>
              <div class="meta">{v['channel']}{views_str} · {v.get('published_at','')[:10]}</div>
              {embed}
              <p>{v.get('description','')[:250]}</p>
            </div>"""

    # TikTok section
    tiktok_html = ""
    if tiktok_videos:
        tiktok_html = "<h2>🎵 TikTok Reviews</h2>"
        for tv in tiktok_videos:
            plays = tv.get("play_count", 0)
            plays_str = f"{int(plays):,} plays" if plays else ""
            tiktok_html += f"""
            <div class="video-card">
              <h3><a href="{tv['url']}" target="_blank">{tv.get('title','TikTok Review')}</a></h3>
              <div class="meta">@{tv.get('author','unknown')} · {plays_str}</div>
            </div>"""

    # Brands / shop section
    brands_html = ""
    if matched_brands:
        brands_html = "<div class='brands-section'><h2>🛍️ Shop the Brands Mentioned</h2>"
        for brand in sorted(set(matched_brands)):
            link = build_affiliate_link(brand, config)
            brand_info = MATERNITY_BRANDS.get(brand, {})
            comm = brand_info.get("commission", "")
            comm_str = f" ({comm} commission)" if comm else ""
            brands_html += f"""
            <span class="brand-item">
              <a href="{link}" target="_blank">{brand.title()}{comm_str}</a>
            </span>"""
        brands_html += "</div>"

    # Footer
    query_list = ", ".join(queries)
    footer = f"""
    <div class="footer">
      <p>Searches performed: {query_list}<br>
      {len(youtube_videos)} YouTube videos · {len(tiktok_videos)} TikTok videos · {len(matched_brands)} brands linked</p>
      <p>Generated by Maternity Wear Review Bot — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
    </div>"""

    html = BLOG_TEMPLATE.format(
        title=title,
        date=datetime.now(timezone.utc).strftime("%B %d, %Y"),
        youtube_section=youtube_html,
        tiktok_section=tiktok_html,
        brands_section=brands_html,
        footer=footer
    )
    return html

# ── State management ────────────────────────────────────────────────────────
def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"seen_video_ids": [], "last_blog_hash": ""}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── Main scan ───────────────────────────────────────────────────────────────
def scan(config, state):
    """Execute one full scan cycle."""
    api_key = config.get("youtube_api_key", "").strip()
    if not api_key:
        post_to_hub(
            "⚠️ YouTube API key not set. Open maternity_review_config.json and add your key from console.cloud.google.com.",
            "error"
        )
        return

    queries = config.get("search_queries", ["maternity clothing try on haul"])
    max_per_query = config.get("max_videos_per_query", 5)
    tiktok_enabled = config.get("tiktok_enabled", False)
    tiktok_token = config.get("tiktok_ms_token", "").strip()

    all_youtube = []
    all_tiktok = []
    all_brands = set()
    new_video_count = 0

    for query in queries:
        post_to_hub(f"🔍 Searching YouTube: \"{query}\"", "info", {"query": query})

        yt_videos = search_youtube(query, api_key, max_per_query)
        time.sleep(0.7)  # rate limit

        # Get statistics for these videos
        video_ids = [v["video_id"] for v in yt_videos]
        stats = get_video_statistics(video_ids, api_key) if video_ids else {}
        time.sleep(0.5)

        # Enrich with stats
        for v in yt_videos:
            vid = v["video_id"]
            if vid in stats:
                v["view_count"] = stats[vid].get("viewCount", 0)
                v["like_count"] = stats[vid].get("likeCount", 0)
                v["comment_count"] = stats[vid].get("commentCount", 0)
            else:
                v["view_count"] = 0
                v["like_count"] = 0
                v["comment_count"] = 0

            # Match brands in title + description
            text = (v.get("title", "") + " " + v.get("description", ""))
            brands = match_brands(text)
            v["matched_brands"] = brands
            all_brands.update(brands)

            # Track new videos
            if vid not in state.get("seen_video_ids", []):
                state.setdefault("seen_video_ids", []).append(vid)
                new_video_count += 1
                post_to_hub(
                    f"📹 New: {v['title'][:100]} — {v['channel']}",
                    "info",
                    {"video_id": vid, "url": v["url"], "matched_brands": brands}
                )

        all_youtube.extend(yt_videos)

        # TikTok (if enabled)
        if tiktok_enabled and tiktok_token:
            tt_videos = search_tiktok(query, tiktok_token, max_per_query)
            for tv in tt_videos:
                brands = match_brands(tv.get("title", ""))
                tv["matched_brands"] = brands
                all_brands.update(brands)

                vid = tv.get("video_id", "")
                if vid and vid not in state.get("seen_video_ids", []):
                    state.setdefault("seen_video_ids", []).append(vid)
                    new_video_count += 1
            all_tiktok.extend(tt_videos)
            time.sleep(1.5)

    # ── Generate blog post ──────────────────────────────────────────────────
    if all_youtube or all_tiktok:
        BLOG_DIR.mkdir(exist_ok=True)
        html = generate_blog_post(all_youtube, all_tiktok, list(all_brands), config, queries)

        # Only save if content changed
        new_hash = hashlib.md5(html.encode()).hexdigest()
        old_hash = state.get("last_blog_hash", "")
        if new_hash != old_hash or new_video_count > 0:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"maternity_reviews_{timestamp}.html"
            filepath = BLOG_DIR / filename
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(html)

            state["last_blog_hash"] = new_hash

            post_to_hub(
                f"📝 Blog post updated! {len(all_youtube)} YT + {len(all_tiktok)} TT videos → {len(all_brands)} brands. Saved to {filepath}",
                "warning",
                {
                    "blog_file": str(filepath),
                    "youtube_count": len(all_youtube),
                    "tiktok_count": len(all_tiktok),
                    "brands_found": sorted(list(all_brands)),
                    "total_videos": len(all_youtube) + len(all_tiktok),
                    "search_queries": queries
                }
            )
        else:
            post_to_hub(
                f"✅ Scan complete — no new videos. {len(all_youtube)} YT + {len(all_tiktok)} TT videos tracked.",
                "info"
            )

    save_state(state)

# ── Entry point ──────────────────────────────────────────────────────────────
def main():
    wait_for_hub()

    if not CFG_FILE.exists():
        with open(CFG_FILE, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        post_to_hub(
            f"Config file created at {CFG_FILE}.\n"
            "★ Add your YouTube Data API v3 key (free from console.cloud.google.com).\n"
            "★ Optionally add your Awin / ShareASale publisher IDs for commissions.\n"
            "★ Set tiktok_enabled=true after installing TikTokApi + playwright.",
            "warning"
        )
        return

    with open(CFG_FILE, "r") as f:
        config = json.load(f)

    post_to_hub(
        "👗 Maternity Wear Review Bot online — aggregating real reviews from YouTube & TikTok.",
        "info",
        {"search_queries": config.get("search_queries", [])}
    )

    state = load_state()
    interval_h = config.get("scan_interval_hours", 12)

    while True:
        try:
            scan(config, state)
        except Exception as e:
            post_to_hub(f"Scan error: {e}", "error")
        time.sleep(interval_h * 3600)

if __name__ == "__main__":
    main()
