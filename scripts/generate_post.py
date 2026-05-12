#!/usr/bin/env python3
"""
Blog post generator for The Dark Psyche.
Pulls recent YouTube videos, generates matching blog posts via Claude API,
embeds Amazon affiliate products, and publishes to WordPress.com.
"""

import os
import json
import base64
import random
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import requests
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── env vars ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
WP_SITE             = os.environ.get("WP_SITE", "darkpsychelab.wordpress.com")
WP_USERNAME         = os.environ["WP_USERNAME"]
WP_APP_PASSWORD     = os.environ["WP_APP_PASSWORD"]
YOUTUBE_TOKEN_B64   = os.environ.get("YOUTUBE_TOKEN_B64", "")
YOUTUBE_API_KEY     = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_ID  = os.environ.get("YOUTUBE_CHANNEL_ID", "")
AMAZON_TAG          = os.environ.get("AMAZON_ASSOCIATE_TAG", "")
AMAZON_ACCESS_KEY   = os.environ.get("AMAZON_ACCESS_KEY", "")
AMAZON_SECRET_KEY   = os.environ.get("AMAZON_SECRET_KEY", "")

CONFIG_PATH = Path(__file__).parent.parent / "config" / "topics.json"
ANALYTICS_PATH = Path(__file__).parent.parent / "config" / "analytics_state.json"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── YouTube helpers ────────────────────────────────────────────────────────────

def get_recent_youtube_videos(max_results: int = 5) -> list[dict]:
    """Return recent videos from the channel using API key (public data)."""
    if not YOUTUBE_API_KEY or not YOUTUBE_CHANNEL_ID:
        log.warning("YOUTUBE_API_KEY or YOUTUBE_CHANNEL_ID not set — skipping YT fetch")
        return []
    try:
        yt = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)
        resp = yt.search().list(
            part="snippet",
            channelId=YOUTUBE_CHANNEL_ID,
            maxResults=max_results,
            order="date",
            type="video",
        ).execute()
        videos = []
        for item in resp.get("items", []):
            s = item["snippet"]
            videos.append({
                "video_id": item["id"]["videoId"],
                "title": s["title"],
                "description": s["description"][:500],
                "published_at": s["publishedAt"],
                "url": f"https://www.youtube.com/watch?v={item['id']['videoId']}",
            })
        return videos
    except Exception as e:
        log.error(f"YouTube API error: {e}")
        return []


# ── Topic selection ────────────────────────────────────────────────────────────

def choose_topic(videos: list[dict], config: dict, analytics: dict) -> dict:
    """
    Pick the best topic for today's post.
    Priority: recent YouTube video > high-performing past topic > random core topic.
    """
    used_topics = set(analytics.get("used_topics", []))
    top_topics = analytics.get("top_performing_topics", [])

    if videos:
        v = videos[0]
        return {
            "type": "youtube_video",
            "title": v["title"],
            "video_url": v["url"],
            "video_id": v["video_id"],
            "seed": v["description"],
        }

    # Prefer topics that performed well before (if not recently used)
    for t in top_topics:
        if t not in used_topics:
            return {"type": "core_topic", "seed": t}

    # Fall back to random unused core topic
    remaining = [t for t in config["core_topics"] if t not in used_topics]
    if not remaining:
        remaining = config["core_topics"]  # reset cycle
    return {"type": "core_topic", "seed": random.choice(remaining)}


# ── Amazon helpers ─────────────────────────────────────────────────────────────

def get_amazon_products(keywords: str, config: dict) -> list[dict]:
    if AMAZON_ACCESS_KEY and AMAZON_SECRET_KEY and AMAZON_TAG:
        return _pa_api_search(keywords)
    return _generate_search_links(keywords, config)


def _generate_search_links(keywords: str, config: dict) -> list[dict]:
    tag = AMAZON_TAG or "darkpsyche-20"
    base = "https://www.amazon.com/s?k={query}&tag={tag}"
    products = []
    kw_list = config.get("amazon_product_keywords", [])
    selected = random.sample(kw_list, min(3, len(kw_list)))
    for kw in selected:
        query = kw.replace(" ", "+")
        products.append({
            "title": kw.title(),
            "url": base.format(query=query, tag=tag),
            "type": "search_link",
        })
    return products


def _pa_api_search(keywords: str) -> list[dict]:
    import hmac, hashlib, urllib.parse

    host = "webservices.amazon.com"
    region = "us-east-1"
    service = "ProductAdvertisingAPI"
    endpoint = f"https://{host}/paapi5/searchitems"

    payload = json.dumps({
        "Keywords": keywords,
        "Resources": ["ItemInfo.Title", "Offers.Listings.Price", "Images.Primary.Medium"],
        "PartnerTag": AMAZON_TAG,
        "PartnerType": "Associates",
        "Marketplace": "www.amazon.com",
        "ItemCount": 3,
    })

    now = datetime.now(timezone.utc)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")

    headers = {
        "Content-Type": "application/json; charset=UTF-8",
        "Host": host,
        "X-Amz-Date": amz_date,
        "X-Amz-Target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems",
    }

    def sign(key, msg):
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    def get_signature_key(key, date_stamp, region, service):
        k_date    = sign(("AWS4" + key).encode("utf-8"), date_stamp)
        k_region  = sign(k_date, region)
        k_service = sign(k_region, service)
        k_signing = sign(k_service, "aws4_request")
        return k_signing

    canonical_headers = "".join(f"{k.lower()}:{v}\n" for k, v in sorted(headers.items()))
    signed_headers = ";".join(sorted(h.lower() for h in headers))
    payload_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()

    canonical_request = "\n".join([
        "POST", "/paapi5/searchitems", "",
        canonical_headers, signed_headers, payload_hash
    ])

    credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", amz_date, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    ])

    signing_key = get_signature_key(AMAZON_SECRET_KEY, date_stamp, region, service)
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={AMAZON_ACCESS_KEY}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={signature}"
    )

    try:
        resp = requests.post(endpoint, data=payload, headers=headers, timeout=10)
        items = resp.json().get("SearchResult", {}).get("Items", [])
        products = []
        for item in items:
            products.append({
                "title": item["ItemInfo"]["Title"]["DisplayValue"],
                "url": f"https://www.amazon.com/dp/{item['ASIN']}?tag={AMAZON_TAG}",
                "asin": item["ASIN"],
                "type": "product",
            })
        return products
    except Exception as e:
        log.error(f"PA API error: {e}")
        return []


# ── Claude post generation ─────────────────────────────────────────────────────

def generate_blog_post(topic: dict, products: list[dict], config: dict) -> dict:
    product_block = ""
    if products:
        product_block = "\n\nRESOURCES TO EMBED (use these as affiliate recommendations):\n"
        for i, p in enumerate(products, 1):
            product_block += f"{i}. [{p['title']}]({p['url']})\n"

    is_video = topic["type"] == "youtube_video"
    context = (
        f"YouTube video to expand upon:\nTitle: {topic['title']}\nURL: {topic['video_url']}\n"
        f"Description excerpt: {topic.get('seed', '')}"
        if is_video else
        f"Topic to write about: {topic['seed']}"
    )

    prompt = f"""You are a writer for "The Dark Psyche" blog — a site focused on dark psychology,
manipulation awareness, and psychological self-defense. The tone is intelligent, direct, and
slightly edgy but never harmful. You educate readers to protect themselves.

Write a complete, SEO-optimized blog post for the following:

{context}
{product_block}

Requirements:
- Title: Compelling, curiosity-driven, SEO-friendly (include a number or "how to" when natural)
- Length: 900-1200 words
- Structure: Intro hook → 4-6 subheadings → conclusion with CTA
- Naturally embed the affiliate product links in context
- End with a call-to-action to subscribe to The Dark Psyche YouTube channel
- Include a 1-2 sentence meta description at the very end labeled "META:"
- Include 5 relevant tags at the very end labeled "TAGS:" (comma separated)
- Include the best category labeled "CATEGORY:" — choose from: {list(config['categories'].keys())}

Output ONLY the blog post content in HTML-ready markdown. Do not include preamble."""

    log.info(f"Generating post for: {topic.get('title') or topic.get('seed')}")
    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text

    meta = ""
    tags = []
    category = "Dark Psychology"

    meta_match = re.search(r"META:\s*(.+)", raw)
    if meta_match:
        meta = meta_match.group(1).strip()
        raw = raw[:meta_match.start()].strip()

    tags_match = re.search(r"TAGS:\s*(.+)", raw)
    if tags_match:
        tags = [t.strip() for t in tags_match.group(1).split(",")]
        raw = raw[:tags_match.start()].strip()

    cat_match = re.search(r"CATEGORY:\s*(.+)", raw)
    if cat_match:
        category = cat_match.group(1).strip()
        raw = raw[:cat_match.start()].strip()

    title = topic.get("title", "Dark Psychology Insights")
    h1_match = re.match(r"^#\s+(.+)$", raw, re.MULTILINE)
    if h1_match:
        title = h1_match.group(1).strip()
        raw = raw[h1_match.end():].strip()

    return {
        "title": title,
        "content": raw,
        "meta": meta,
        "tags": tags,
        "category": category,
        "topic_seed": topic.get("title") or topic.get("seed"),
    }


# ── WordPress publishing ───────────────────────────────────────────────────────

def get_wp_category_id(category_name: str, config: dict) -> int | None:
    slug = config["categories"].get(category_name, "")
    url = f"https://public-api.wordpress.com/wp/v2/sites/{WP_SITE}/categories"
    resp = requests.get(url, params={"slug": slug}, timeout=10)
    items = resp.json()
    if items and isinstance(items, list):
        return items[0]["id"]
    return None


def publish_to_wordpress(post: dict, config: dict) -> str:
    auth = base64.b64encode(f"{WP_USERNAME}:{WP_APP_PASSWORD}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }
    api_base = f"https://public-api.wordpress.com/wp/v2/sites/{WP_SITE}"

    cat_id = get_wp_category_id(post["category"], config)

    tag_ids = []
    for tag_name in post.get("tags", []):
        resp = requests.post(
            f"{api_base}/tags",
            headers=headers,
            json={"name": tag_name},
            timeout=10,
        )
        data = resp.json()
        tag_id = data.get("id") or data.get("term_id")
        if tag_id:
            tag_ids.append(tag_id)

    payload = {
        "title": post["title"],
        "content": post["content"],
        "status": "publish",
        "excerpt": post.get("meta", ""),
        "categories": [cat_id] if cat_id else [],
        "tags": tag_ids,
    }

    resp = requests.post(f"{api_base}/posts", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    post_url = result.get("link", "")
    log.info(f"Published: {post['title']} → {post_url}")
    return post_url


# ── Analytics state ────────────────────────────────────────────────────────────

def load_analytics() -> dict:
    if ANALYTICS_PATH.exists():
        return json.loads(ANALYTICS_PATH.read_text())
    return {"used_topics": [], "top_performing_topics": [], "posts_this_week": 0, "weekly_target": 3}


def save_analytics(analytics: dict, topic_seed: str) -> None:
    used = analytics.get("used_topics", [])
    used.append(topic_seed)
    if len(used) > 50:
                used = used[-50:]
    analytics["used_topics"] = used
    analytics["posts_this_week"] = analytics.get("posts_this_week", 0) + 1
    analytics["last_post_date"] = datetime.now(timezone.utc).isoformat()
    ANALYTICS_PATH.write_text(json.dumps(analytics, indent=2))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    config   = json.loads(CONFIG_PATH.read_text())
    analytics = load_analytics()

    videos  = get_recent_youtube_videos(max_results=3)
    topic   = choose_topic(videos, config, analytics)
    log.info(f"Selected topic: {topic}")

    products = get_amazon_products(topic.get("seed", "dark psychology"), config)
    log.info(f"Found {len(products)} Amazon products")

    post = generate_blog_post(topic, products, config)
    url  = publish_to_wordpress(post, config)

    save_analytics(analytics, post["topic_seed"])
    log.info(f"Done. Post live at: {url}")
    print(f"::notice::Published: {post['title']} → {url}")


if __name__ == "__main__":
    main()
