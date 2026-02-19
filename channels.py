"""
channels.py — YouTube channel monitoring via RSS feeds.

Primary method: YouTube Atom/RSS feeds (no API key required).
Optional enrichment: YouTube Data API v3 (enabled via YOUTUBE_API_ENABLED).

Each channel in config.CHANNELS is polled every POLL_INTERVAL_SECONDS.
New video IDs not present in the database are returned for processing.
"""

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

import feedparser
import requests

import config
import database

logger = logging.getLogger(__name__)

# YouTube RSS feed URL template
_RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

# YouTube Data API endpoint (only used when YOUTUBE_API_ENABLED=True)
_YT_API_VIDEO_URL = "https://www.googleapis.com/youtube/v3/videos"


@dataclass
class VideoEntry:
    """Represents a newly discovered video ready for processing."""
    video_id: str
    channel_id: str
    channel_name: str
    title: str
    published_at: Optional[datetime]
    description: str = ""
    # These are populated by YouTube API enrichment (optional)
    duration_seconds: Optional[int] = None
    view_count: Optional[int] = None
    tags: list[str] = None

    def __post_init__(self):
        if self.tags is None:
            self.tags = []


# ────────────────────────────────────────────────────────────
#  RSS feed polling
# ────────────────────────────────────────────────────────────

def _parse_published(entry) -> Optional[datetime]:
    """Extract a timezone-aware datetime from a feedparser entry."""
    # feedparser provides published_parsed (struct_time, UTC) when available
    if hasattr(entry, "published_parsed") and entry.published_parsed:
        try:
            return datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    # Fallback: try raw published string
    if hasattr(entry, "published") and entry.published:
        try:
            return parsedate_to_datetime(entry.published)
        except Exception:
            pass
    return None


def _is_too_old(published_at: Optional[datetime]) -> bool:
    """Return True if the video is older than MAX_VIDEO_AGE_DAYS."""
    if config.MAX_VIDEO_AGE_DAYS == 0 or published_at is None:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(days=config.MAX_VIDEO_AGE_DAYS)
    return published_at < cutoff


def fetch_new_videos_from_rss(channel_id: str, channel_name: str) -> list[VideoEntry]:
    """
    Fetch the RSS feed for a channel and return VideoEntry objects for
    any videos not already in the database.
    """
    url = _RSS_URL.format(channel_id=channel_id)
    logger.debug("Fetching RSS for %s (%s)", channel_name, channel_id)

    # ── Let feedparser fetch the URL directly using its own HTTP client.
    #    This is intentional: feedparser's urllib-based fetching is accepted
    #    by YouTube's RSS endpoint, whereas requests (even with browser
    #    user-agent spoofing) gets blocked with 404s. The seeder uses the
    #    same approach and works reliably.
    try:
        feed = feedparser.parse(url)
    except Exception as exc:
        logger.error("RSS parse error for %s: %s", channel_name, exc)
        database.increment_channel_error(channel_id)
        return []

    http_status = getattr(feed, "status", None)

    if feed.bozo and feed.bozo_exception:
        # feedparser sets bozo=True for malformed XML — usually means YouTube
        # returned an error page instead of a feed. Log the HTTP status for
        # diagnosis. If it's a 4xx/5xx we bail out early.
        logger.warning(
            "Bozo feed for %s: %s | HTTP %s",
            channel_name,
            feed.bozo_exception,
            http_status,
        )
        if http_status and http_status >= 400:
            database.increment_channel_error(channel_id)
            return []

    new_videos: list[VideoEntry] = []

    for entry in feed.entries:
        video_id = getattr(entry, "yt_videoid", None)
        if not video_id:
            continue

        if database.is_video_known(video_id):
            continue

        published_at = _parse_published(entry)

        if _is_too_old(published_at):
            logger.debug(
                "Skipping old video %s (%s) published %s",
                video_id, entry.title, published_at
            )
            # Still mark as known so we don't re-check every poll
            database.insert_video(
                video_id=video_id,
                channel_id=channel_id,
                channel_name=channel_name,
                title=entry.get("title", "Untitled"),
                published_at=published_at,
            )
            database.update_summary(video_id, status="skipped")
            continue

        description = getattr(entry, "summary", "") or ""

        new_videos.append(VideoEntry(
            video_id=video_id,
            channel_id=channel_id,
            channel_name=channel_name,
            title=entry.get("title", "Untitled"),
            published_at=published_at,
            description=description,
        ))

    if new_videos:
        logger.info(
            "Found %d new video(s) from %s", len(new_videos), channel_name
        )

    database.mark_channel_checked(channel_id)
    return new_videos


# ────────────────────────────────────────────────────────────
#  Optional: YouTube Data API enrichment
# ────────────────────────────────────────────────────────────

def _enrich_with_youtube_api(video: VideoEntry) -> VideoEntry:
    """
    Fetch additional metadata from the YouTube Data API for a single video.
    Only called when YOUTUBE_API_ENABLED=True.

    Cost: 1 quota unit per call (free tier: 10,000 units/day).
    """
    if not config.YOUTUBE_API_KEY:
        logger.warning("YOUTUBE_API_ENABLED is True but YOUTUBE_API_KEY is not set.")
        return video

    try:
        resp = requests.get(
            _YT_API_VIDEO_URL,
            params={
                "key": config.YOUTUBE_API_KEY,
                "id": video.video_id,
                "part": "contentDetails,statistics,snippet",
            },
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        items = data.get("items", [])
        if not items:
            logger.warning("YouTube API returned no items for %s", video.video_id)
            return video

        item = items[0]
        snippet = item.get("snippet", {})
        stats = item.get("statistics", {})
        content = item.get("contentDetails", {})

        # Enrich the VideoEntry in place
        video.tags = snippet.get("tags", [])
        video.view_count = int(stats.get("viewCount", 0))

        # Parse ISO 8601 duration (e.g. "PT1H2M3S") to seconds
        raw_duration = content.get("duration", "")
        video.duration_seconds = _parse_iso8601_duration(raw_duration)

    except requests.RequestException as exc:
        logger.error("YouTube API error for %s: %s", video.video_id, exc)

    return video


def _parse_iso8601_duration(duration: str) -> Optional[int]:
    """Convert 'PT1H2M3S' to total seconds. Returns None on failure."""
    import re
    if not duration:
        return None
    pattern = r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?"
    match = re.match(pattern, duration)
    if not match:
        return None
    hours, minutes, seconds = (int(x) if x else 0 for x in match.groups())
    return hours * 3600 + minutes * 60 + seconds


# ────────────────────────────────────────────────────────────
#  Main entry point called by monitor.py
# ────────────────────────────────────────────────────────────

def poll_all_channels() -> list[VideoEntry]:
    """
    Poll all configured channels and return a list of new VideoEntry objects.
    Inserts newly discovered videos into the database as a side effect.
    """
    if not config.CHANNELS:
        logger.warning(
            "No channels configured. Add channel IDs to CHANNELS in config.py."
        )
        return []

    all_new_videos: list[VideoEntry] = []

    for channel_id, channel_name in config.CHANNELS.items():
        database.upsert_channel(channel_id, channel_name)
        new_videos = fetch_new_videos_from_rss(channel_id, channel_name)

        for video in new_videos:
            # Persist to DB immediately so duplicate runs don't re-process
            database.insert_video(
                video_id=video.video_id,
                channel_id=video.channel_id,
                channel_name=video.channel_name,
                title=video.title,
                published_at=video.published_at,
            )

            # Optional API enrichment
            if config.YOUTUBE_API_ENABLED:
                video = _enrich_with_youtube_api(video)

            all_new_videos.append(video)

        # Brief pause between channels to be polite to YouTube's servers
        if len(config.CHANNELS) > 1:
            time.sleep(0.5)

    logger.info(
        "Poll complete — %d new video(s) across %d channel(s)",
        len(all_new_videos), len(config.CHANNELS)
    )
    return all_new_videos
