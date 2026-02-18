"""
transcripts.py — Three-tier transcript extraction for yt-monitor.

Tier 1: youtube-transcript-api (auto-generated or manual captions, no auth)
Tier 2: Whisper audio transcription via yt-dlp (enabled by WHISPER_ENABLED)
Tier 3: Metadata-only fallback (title + description from RSS feed)

Each tier is attempted in order. The first successful result is returned
along with the tier number so the summarizer can adjust its prompt.
Proxy support (residential IP rotation via Webshare) is built in and
activated by setting PROXY_ENABLED = True in config.py.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass

import requests
from youtube_transcript_api import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
    YouTubeTranscriptApi,
)

import config

logger = logging.getLogger(__name__)


# ────────────────────────────────────────────────────────────
#  Result dataclass
# ────────────────────────────────────────────────────────────

@dataclass
class TranscriptResult:
    """
    Holds the extracted transcript text and the tier it came from.

    tier 1 — youtube-transcript-api (manual or auto captions)
    tier 2 — Whisper audio transcription
    tier 3 — Metadata-only fallback (title + description)
    """
    text: str
    tier: int


# ────────────────────────────────────────────────────────────
#  Proxy management
# ────────────────────────────────────────────────────────────

_proxy_list: list[str] = []


def _load_proxies() -> list[str]:
    """Download the current proxy list from Webshare and return as proxy URLs."""
    logger.info("Loading proxy list from Webshare...")
    response = requests.get(config.WEBSHARE_DOWNLOAD_URL, timeout=10)
    response.raise_for_status()

    proxies = []
    for line in response.text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        # Each line format: ip:port:username:password
        parts = line.split(":")
        if len(parts) == 4:
            ip, port, username, password = parts
            proxies.append(f"http://{username}:{password}@{ip}:{port}")

    if not proxies:
        raise ValueError("No proxies returned from Webshare — check your download URL.")

    logger.info("Loaded %d proxies from Webshare.", len(proxies))
    return proxies


def init_proxies() -> None:
    """Call once at app startup to pre-load the proxy list (no-op if PROXY_ENABLED=False)."""
    global _proxy_list
    if not config.PROXY_ENABLED:
        logger.info("Proxy support is disabled — skipping proxy load.")
        return

    try:
        _proxy_list = _load_proxies()
    except Exception as exc:
        logger.error(
            "Failed to load proxy list from Webshare: %s — "
            "transcript requests will proceed WITHOUT a proxy until the next successful load. "
            "Check your WEBSHARE_DOWNLOAD_URL in config.py.",
            exc,
        )
        _proxy_list = []  # fall back to direct requests rather than crashing


def _pick_proxy(attempted: set[str]) -> dict | None:
    """
    Pick a proxy from the loaded list, avoiding already-attempted ones.
    Returns None if proxies are disabled or the list is empty.
    """
    if not config.PROXY_ENABLED or not _proxy_list:
        return None

    available = [p for p in _proxy_list if p not in attempted]
    if not available:
        attempted.clear()  # exhausted the list — start over
        available = _proxy_list

    proxy_url = random.choice(available)
    attempted.add(proxy_url)
    return {"http": proxy_url, "https": proxy_url}


# ────────────────────────────────────────────────────────────
#  Tier 1 — youtube-transcript-api
# ────────────────────────────────────────────────────────────

from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled, VideoUnavailable
from youtube_transcript_api.proxies import WebshareProxyConfig

def init_proxies() -> None:
    """No-op — WebshareProxyConfig handles rotation internally."""
    if config.PROXY_ENABLED:
        if not config.PROXY_USERNAME or not config.PROXY_PASSWORD:
            logger.error("PROXY_ENABLED=True but PROXY_USERNAME or PROXY_PASSWORD is not set in .env.")
        else:
            logger.info("Proxy support enabled via WebshareProxyConfig.")
    else:
        logger.info("Proxy support is disabled.")


def _make_api() -> YouTubeTranscriptApi:
    """Instantiate the API client, with or without proxy."""
    if config.PROXY_ENABLED and config.PROXY_USERNAME and config.PROXY_PASSWORD:
        return YouTubeTranscriptApi(
            proxy_config=WebshareProxyConfig(
                proxy_username=config.PROXY_USERNAME,
                proxy_password=config.PROXY_PASSWORD,
            )
        )
    return YouTubeTranscriptApi()


def get_transcript(
    video_id: str,
    title: str = "",
    description: str = "",
    languages: list[str] = ["en"],
    max_retries: int = 5,
    backoff_base: float = 4.0,  # was 2.0 — doubles each wait time
) -> TranscriptResult:
    last_exception = None

    for attempt in range(max_retries):
        try:
            ytt = _make_api()
            fetched = ytt.fetch(video_id, languages=languages)
            if attempt > 0:
                logger.info("[%s] Transcript fetched successfully on attempt %d.", video_id, attempt + 1)
            return fetched.to_raw_data()

        except (TranscriptsDisabled, NoTranscriptFound):
            logger.warning("[%s] No transcript available (disabled or not found).", video_id)
            return None

        except VideoUnavailable:
            logger.warning("[%s] Video is unavailable.", video_id)
            return None

        except Exception as exc:
            last_exception = exc
            wait = backoff_base ** attempt + random.uniform(0, 1)
            logger.warning(
                "[%s] Attempt %d/%d failed (%s: %s). Retrying in %.1fs...",
                video_id, attempt + 1, max_retries, type(exc).__name__, exc, wait,
            )
            time.sleep(wait)

    logger.error("[%s] All %d attempts failed. Last error: %s", video_id, max_retries, last_exception)
    return None


def _segments_to_text(segments: list[dict]) -> str:
    """Join transcript segments into a single plain-text string."""
    return " ".join(s.get("text", "") for s in segments).strip()


# ────────────────────────────────────────────────────────────
#  Tier 2 — Whisper (stub; enabled by WHISPER_ENABLED)
# ────────────────────────────────────────────────────────────

def _fetch_via_whisper(video_id: str) -> str | None:
    """
    Download audio with yt-dlp and transcribe with Whisper.
    Returns transcript text on success, None on failure.

    ⚠️  Requires WHISPER_ENABLED=True, yt-dlp, and either:
          - openai-whisper (local) or
          - OPENAI_API_KEY (API)
        See README for setup instructions.
    """
    if not config.WHISPER_ENABLED:
        return None

    try:
        import yt_dlp  # noqa: F401 — checked at runtime
    except ImportError:
        logger.warning("Whisper enabled but yt-dlp is not installed. Skipping Tier 2.")
        return None

    logger.info("[%s] Tier 2: attempting Whisper transcription...", video_id)
    # Full Whisper implementation goes here when WHISPER_ENABLED is activated.
    # See README: Enabling Optional Features → Whisper Tier-2 transcription
    logger.warning("[%s] Whisper transcription is not yet fully implemented.", video_id)
    return None


# ────────────────────────────────────────────────────────────
#  Tier 3 — Metadata fallback
# ────────────────────────────────────────────────────────────

def _build_metadata_fallback(title: str, description: str) -> str:
    """Construct a minimal text blob from video metadata for Tier 3."""
    parts = []
    if title:
        parts.append(f"Title: {title}")
    if description:
        parts.append(f"Description: {description}")
    return "\n\n".join(parts) if parts else "No metadata available."


# ────────────────────────────────────────────────────────────
#  Public entry point
# ────────────────────────────────────────────────────────────

def get_transcript(
    video_id: str,
    title: str = "",
    description: str = "",
    languages: list[str] = ["en"],
    max_retries: int = 5,
    backoff_base: float = 2.0,
) -> TranscriptResult:
    """
    Extract a transcript using a 3-tier fallback pipeline.

    Tier 1: youtube-transcript-api (with optional proxy rotation)
    Tier 2: Whisper audio transcription (if WHISPER_ENABLED=True)
    Tier 3: Metadata-only fallback (always succeeds)

    Args:
        video_id:     YouTube video ID
        title:        Video title (used for Tier 3 fallback)
        description:  Video description (used for Tier 3 fallback)
        languages:    Preferred transcript languages in priority order
        max_retries:  Max Tier 1 attempts before falling through
        backoff_base: Base seconds for exponential backoff

    Returns:
        TranscriptResult with .text and .tier set.
    """
    # ── Tier 1 ───────────────────────────────────────────────
    logger.info("[%s] Tier 1: attempting youtube-transcript-api...", video_id)
    segments = _fetch_via_api(video_id, languages, max_retries, backoff_base)
    if segments:
        text = _segments_to_text(segments)
        logger.info("[%s] Tier 1 succeeded (%d chars).", video_id, len(text))
        return TranscriptResult(text=text, tier=1)

    # ── Tier 2 ───────────────────────────────────────────────
    whisper_text = _fetch_via_whisper(video_id)
    if whisper_text:
        logger.info("[%s] Tier 2 (Whisper) succeeded (%d chars).", video_id, len(whisper_text))
        return TranscriptResult(text=whisper_text, tier=2)

    # ── Tier 3 ───────────────────────────────────────────────
    logger.warning("[%s] Falling back to Tier 3 (metadata only).", video_id)
    fallback_text = _build_metadata_fallback(title, description)
    return TranscriptResult(text=fallback_text, tier=3)
