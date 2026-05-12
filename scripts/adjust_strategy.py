#!/usr/bin/env python3
"""
Weekly analytics reader and self-adjustment algorithm for The Dark Psyche blog.
Reads WordPress.com Stats + YouTube Analytics → updates posting strategy.
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anthropic
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
WP_SITE           = os.environ.get("WP_SITE", "darkpsychelab.wordpress.com")
WP_TOKEN          = os.environ["WP_TOKEN"]
YOUTUBE_API_KEY   = os.environ.get("YOUTUBE_API_KEY", "")
YOUTUBE_CHANNEL_ID = os.environ.get("YOUTUBE_CHANNEL_ID", "")

CONFIG_PATH    = Path(__file__).parent.parent / "config" / "topics.json"
ANALYTICS_PATH = Path(__file__).parent.parent / "config" / "analytics_state.json"

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ── WordPress Stats API ────────────────────────────────────────────────────────

def get_wp_stats(days: int = 7) -> dict:
    """Fetch WordPress.com site stats for the past N days."""
    headers = {"Authorization": f"Bearer {WP_TOKEN}"}
    base = f"https://public-api.wordpress.com/rest/v1.1/sites/{WP_SITE}/stats"

    stats = {}
    try:
        # Overall summary
        resp = requests.get(f"{base}/summary", headers=headers, timeout=10)
        stats["summary"] = resp.json()

        # Top posts by views this week
        resp = requests.get(
            f"{base}/top-posts",
            headers=headers,
            params={"period": "week", "num": 10},
            timeout=10,
        )
        stats["top_posts"] = resp.json().get("days", {})

        # Clicks (affiliate link tracking)
        resp = requests.get(
            f"{base}/clicks",
            headers=headers,
            params={"period": "week", "num": 1},
            timeout=10,
        )
        stats["clicks"] = resp.json().get("days", {})

        # Search terms driving traffic
        resp = requests.get(
            f"{base}/search-terms",
            headers=headers,
            params={"period": "week", "num": 1},
            timeout=10,
        )
        stats["search_terms"] = resp.json().get("days", {})

        log.info("WordPress stats fetched successfully")
    except Exception as e:
        log.error(f"WP stats error: {e}")

    return stats


# ── YouTube Channel Stats ──────────────────────────────────────────────────────

def get_youtube_stats() -> dict:
    """Fetch recent YouTube video performance data."""
    if not YOUTUBE_API_KEY or not YOUTUBE_CHANNEL_ID:
        return {}
    try:
        from googleapiclient.discovery import build
        yt = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

        # Get recent videos
        search_resp = yt.search().list(
            part="snippet",
            channelId=YOUTUBE_CHANNEL_ID,
            maxResults=10,
            order="date",
            type="video",
        ).execute()

        video_ids = [i["id"]["videoId"] for i in search_resp.get("items", [])]
        if not video_ids:
            return {}

        # Get video stats
        stats_resp = yt.videos().list(
            part="statistics,snippet",
            id=",".join(video_ids),
        ).execute()

        videos = []
        for item in stats_resp.get("items", []):
            s = item.get("statistics", {})
            videos.append({
                "title": item["snippet"]["title"],
                "video_id": item["id"],
                "views": int(s.get("viewCount", 0)),
                "likes": int(s.get("likeCount", 0)),
                "comments": int(s.get("commentCount", 0)),
                "published_at": item["snippet"]["publishedAt"],
            })

        # Sort by views descending
        videos.sort(key=lambda x: x["views"], reverse=True)
        return {"top_videos": videos[:5]}

    except Exception as e:
        log.error(f"YouTube stats error: {e}")
        return {}


# ── Strategy adjustment ────────────────────────────────────────────────────────

def analyze_and_adjust(wp_stats: dict, yt_stats: dict, config: dict, analytics: dict) -> dict:
    """
    Use Claude to analyze the week's performance data and recommend adjustments
    to posting frequency, topic focus, and Amazon product categories.
    """
    prompt = f"""You are the content strategist for "The Dark Psyche" — a dark psychology blog
and YouTube channel. Analyze the following weekly performance data and output a JSON strategy update.

WORDPRESS STATS (past 7 days):
{json.dumps(wp_stats, indent=2)[:3000]}

YOUTUBE STATS (recent videos):
{json.dumps(yt_stats, indent=2)[:2000]}

CURRENT CONFIG:
- Posts per week target: {analytics.get('weekly_target', 3)}
- Current posting days: {config['posting_schedule']['default_days']}
- Top performing past topics: {analytics.get('top_performing_topics', [])}

ANALYSIS TASKS:
1. Identify which topics/posts got the most views and clicks
2. Identify which YouTube videos performed best (potential blog expansion topics)
3. Determine if posting frequency should increase (high traffic = post more) or decrease (low engagement = post less and focus on quality)
4. Identify which Amazon product categories got the most affiliate clicks
5. Suggest 3-5 new specific blog post topics for next week based on trends

Output ONLY valid JSON in this exact format:
{{
  "weekly_target": <2-5>,
  "posting_days": ["Monday", "Wednesday", "Friday"],
  "top_performing_topics": ["topic1", "topic2", "topic3"],
  "next_week_topics": ["specific topic 1", "specific topic 2", "specific topic 3"],
  "amazon_focus_keywords": ["keyword1", "keyword2", "keyword3"],
  "strategy_note": "one sentence explaining the key insight",
  "frequency_reason": "one sentence explaining why frequency was adjusted"
}}"""

    log.info("Asking Claude to analyze performance and adjust strategy...")
    message = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=800,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()

    # Extract JSON if wrapped in code block
    import re
    json_match = re.search(r"\{.*\}", raw, re.DOTALL)
    if json_match:
        raw = json_match.group(0)

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.error(f"Could not parse Claude strategy response: {raw}")
        return {}


def apply_strategy(strategy: dict, config: dict, analytics: dict) -> None:
    """Write strategy updates back to config and analytics state files."""
    if not strategy:
        log.warning("No strategy to apply")
        return

    # Update posting schedule in config
    if "posting_days" in strategy:
        config["posting_schedule"]["default_days"] = strategy["posting_days"]
    if "amazon_focus_keywords" in strategy:
        config["amazon_product_keywords"] = strategy["amazon_focus_keywords"]

    CONFIG_PATH.write_text(json.dumps(config, indent=2))
    log.info(f"Config updated: {strategy.get('posting_days')} | {strategy.get('weekly_target')} posts/week")

    # Update analytics state
    analytics["weekly_target"]        = strategy.get("weekly_target", 3)
    analytics["top_performing_topics"] = strategy.get("top_performing_topics", [])
    analytics["posts_this_week"]      = 0  # reset weekly counter
    analytics["week_start"]           = datetime.now(timezone.utc).isoformat()
    analytics["last_strategy_note"]   = strategy.get("strategy_note", "")
    analytics["next_week_topics"]     = strategy.get("next_week_topics", [])

    ANALYTICS_PATH.write_text(json.dumps(analytics, indent=2))
    log.info(f"Analytics updated. Strategy: {strategy.get('strategy_note')}")

    # Output to GitHub Actions summary
    summary_lines = [
        "## 📊 Weekly Strategy Update — The Dark Psyche",
        f"**Posts/week target:** {strategy.get('weekly_target', 3)}",
        f"**Posting days:** {', '.join(strategy.get('posting_days', []))}",
        f"**Strategy:** {strategy.get('strategy_note', '')}",
        f"**Frequency reason:** {strategy.get('frequency_reason', '')}",
        "",
        "### Next week's planned topics:",
    ]
    for t in strategy.get("next_week_topics", []):
        summary_lines.append(f"- {t}")

    summary = "\n".join(summary_lines)

    # Write to GitHub step summary if available
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY", "")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(summary + "\n")
    else:
        print(summary)


# ── Cron schedule generator ────────────────────────────────────────────────────

def generate_cron_schedule(days: list[str]) -> str:
    """Convert day names to a cron expression for GitHub Actions."""
    day_map = {
        "Sunday": 0, "Monday": 1, "Tuesday": 2, "Wednesday": 3,
        "Thursday": 4, "Friday": 5, "Saturday": 6,
    }
    day_nums = sorted(day_map[d] for d in days if d in day_map)
    day_str = ",".join(str(d) for d in day_nums)
    return f"0 14 * * {day_str}"  # 2pm UTC = ~4am HST


def update_workflow_schedule(new_days: list[str]) -> None:
    """Update the post_blog.yml cron schedule in-place."""
    workflow_path = Path(__file__).parent.parent / ".github" / "workflows" / "post_blog.yml"
    if not workflow_path.exists():
        return
    cron = generate_cron_schedule(new_days)
    content = workflow_path.read_text()
    import re
    content = re.sub(
        r"(- cron: ')([^']+)('  # auto-managed)",
        f"\\g<1>{cron}\\g<3>",
        content,
    )
    workflow_path.write_text(content)
    log.info(f"Workflow cron updated to: {cron}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    config    = json.loads(CONFIG_PATH.read_text())
    analytics = json.loads(ANALYTICS_PATH.read_text()) if ANALYTICS_PATH.exists() else {}

    log.info("Fetching WordPress stats...")
    wp_stats = get_wp_stats(days=7)

    log.info("Fetching YouTube stats...")
    yt_stats = get_youtube_stats()

    log.info("Analyzing with Claude and generating strategy...")
    strategy = analyze_and_adjust(wp_stats, yt_stats, config, analytics)

    apply_strategy(strategy, config, analytics)

    if strategy.get("posting_days"):
        update_workflow_schedule(strategy["posting_days"])

    log.info("Weekly strategy adjustment complete.")


if __name__ == "__main__":
    main()
