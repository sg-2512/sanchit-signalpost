"""YouTube Data API v3 connector for the SignalPost live agent.

Searches for company YouTube channels and fetches channel statistics.
Activates only when YOUTUBE_API_KEY environment variable is set.

Cost: Free tier (10,000 units/day). Search = 100 units each.
For 100 companies: 10,000 units = exactly the daily free limit.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any


def is_available() -> bool:
    """Check if YouTube API key is configured."""
    return bool(os.environ.get("YOUTUBE_API_KEY"))


def fetch_youtube_data(
    org: str,
    company_name: str,
    social_links: list[dict[str, str]] | None = None,
    *,
    budget: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch YouTube channel data for a company.

    First checks if a YouTube link was discovered on the company website.
    If not, searches YouTube for the company name.

    Returns observation dicts for external footprint evaluation.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        return []

    if budget and not budget.can_proceed():
        return []

    import urllib.parse
    import urllib.request
    import json

    retrieved_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # Check if we already have a YouTube link from the company website
    channel_id = None
    known_url = None
    for link in (social_links or []):
        if link.get("platform") == "youtube":
            url = link.get("url", "")
            known_url = url
            # Extract channel ID from URL patterns like /channel/UCxxxxx
            if "/channel/" in url:
                parts = url.split("/channel/")
                if len(parts) > 1:
                    channel_id = parts[1].split("/")[0]
            break

    if not channel_id:
        # Search YouTube for the company
        clean_name = re.sub(r"\b(AS|ASA|BA|DA|ANS|ENK|NUF|SF|IKS|KF)\b", "", company_name, flags=re.I).strip()
        search_query = clean_name if len(clean_name) >= 3 else company_name.strip()
        search_url = (
            f"https://www.googleapis.com/youtube/v3/search"
            f"?q={urllib.parse.quote(search_query + ' Norway')}"
            f"&type=channel"
            f"&regionCode=NO"
            f"&maxResults=1"
            f"&part=snippet"
            f"&key={api_key}"
        )
        try:
            req = urllib.request.Request(search_url, headers={"User-Agent": "SignalpostAgent/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                search_raw = resp.read()
                search_data = json.loads(search_raw)

            if budget:
                budget.record_request(cost_usd=0.005, bytes_received=len(search_raw))

            items = search_data.get("items", [])
            if not items:
                return []

            channel_id = items[0].get("id", {}).get("channelId")
            if not channel_id:
                return []

        except Exception:
            if budget:
                budget.record_request()
            return []

    # Fetch channel statistics
    stats_url = (
        f"https://www.googleapis.com/youtube/v3/channels"
        f"?id={channel_id}"
        f"&part=statistics,snippet"
        f"&key={api_key}"
    )

    try:
        req = urllib.request.Request(stats_url, headers={"User-Agent": "SignalpostAgent/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            stats_raw = resp.read()
            stats_data = json.loads(stats_raw)

        if budget:
            budget.record_request(cost_usd=0.002, bytes_received=len(stats_raw))

        items = stats_data.get("items", [])
        if not items:
            return []

        channel = items[0]
        stats = channel.get("statistics", {})
        snippet = channel.get("snippet", {})

        subscriber_count = int(stats.get("subscriberCount", 0))
        video_count = int(stats.get("videoCount", 0))
        view_count = int(stats.get("viewCount", 0))
        channel_title = snippet.get("title", "")
        channel_url = known_url or f"https://youtube.com/channel/{channel_id}"

        digest = hashlib.sha256(stats_raw).hexdigest()

        observations: list[dict[str, Any]] = []

        # Profile metrics observation
        observations.append({
            "id": f"yt-{org}-{hashlib.sha256(f'{org}|youtube|{channel_id}'.encode()).hexdigest()[:16]}",
            "organisation_number": org,
            "platform": "youtube",
            "signal_type": "profile_metrics",
            "source_url": channel_url,
            "retrieved_at": retrieved_at,
            "content_sha256": digest,
            "exact_entity": True,
            "identity_proof": [
                {"type": "youtube_channel_match", "channel_id": channel_id, "channel_title": channel_title},
            ],
            "acquisition_mode": "official_api",
            "rights_status": "approved",
            "source_class": "youtube",
            "evidence_span": f"YouTube channel: {channel_title} — {subscriber_count} subscribers, {video_count} videos, {view_count} views",
            "metrics": {
                "channel_id": channel_id,
                "channel_title": channel_title,
                "subscriber_count": subscriber_count,
                "video_count": video_count,
                "view_count": view_count,
            },
            "strategy": "youtube_data_api",
        })

        # Buzz metrics observation
        if video_count > 0 or view_count > 0:
            observations.append({
                "id": f"yt-buzz-{org}-{hashlib.sha256(f'{org}|yt_buzz|{channel_id}'.encode()).hexdigest()[:16]}",
                "organisation_number": org,
                "platform": "youtube",
                "signal_type": "buzz_metrics",
                "source_url": channel_url,
                "retrieved_at": retrieved_at,
                "content_sha256": digest,
                "exact_entity": True,
                "identity_proof": [
                    {"type": "youtube_channel_match", "channel_id": channel_id, "channel_title": channel_title},
                ],
                "acquisition_mode": "official_api",
                "rights_status": "approved",
                "source_class": "youtube",
                "evidence_span": f"YouTube activity: {video_count} videos with {view_count} total views",
                "metrics": {
                    "video_count": video_count,
                    "view_count": view_count,
                    "subscriber_count": subscriber_count,
                },
                "strategy": "youtube_data_api",
            })

        return observations

    except Exception:
        if budget:
            budget.record_request()
        return []
