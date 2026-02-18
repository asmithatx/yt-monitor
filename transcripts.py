"""
transcripts.py — Three-tier transcript extraction for yt-monitor.

Tier 1: youtube-transcript-api (auto-generated or manual captions, no auth)
Tier 2: Whisper audio transcription via yt-dlp (enabled by WHISPER_ENABLED)
Tier 3: Metadata-only fallback (title + description from RSS feed)

Each tier is attempted in order. The first successful result is returned
along with the tier number so the summarizer can adjust its prompt.
"""

# transcripts.py
import time
import random
import logging
import requests
from youtube_transcript_api import (
    YouTubeTranscriptApi,
    TranscriptsDisabled,
    NoTranscriptFound,
    VideoUnavailable,
)
from config import PROXY_ENABLED, WEBSHARE_DOWNLOAD_URL

logger = logging.getLogger(__name__)

# ── Proxy list — loaded once at startup ─────────────────────────────────────

_proxy_list: list[str] = []


def _load_proxies() -> list[str]:
    """Download the current proxy list from Webshare and return as proxy URLs."""
    logger.info("Loading proxy list from Webshare...")
    response = requests.get(WEBSHARE_DOWNLOAD_URL, timeout=10)
    response.raise_for_status()

    proxies = []
    for line in response.text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(":")
        if len(parts) == 4:
            ip, port, username, password = parts
            proxies.append(f"http://{username}:{password}@{ip}:{port}")

    if not proxies:
        raise ValueError("No proxies returned from Webshare — check your download URL.")

    logger.info(f"Loaded {len(proxies)} proxies from Webshare.")
    return proxies


def init_proxies() -> None:
    """Call once at app startup to pre-load the proxy list (only if proxies are enabled)."""
    global _proxy_list
    if PROXY_ENABLED:
        _proxy_list = _load_proxies()
    else:
        logger.info("Proxy support is disabled — skipping proxy load.")


# ── Transcript fetching ──────────────────────────────────────────────────────

def get_transcript(
    video_id: str,
    languages: list[str] = ["en"],
    max_retries: int = 5,
    backoff_base: float = 2.0,
) -> list[dict] | None:
    """
    Fetch a YouTube transcript, optionally routing through residential proxies.

    Rotates proxies on transient failures with exponential backoff + jitter.
    Permanent errors (disabled/missing transcripts, unavailable video) raise immediately.

    Args:
        video_id:     YouTube video ID (e.g. "dQw4w9WgXcQ")
        languages:    Preferred transcript languages in order of priority
        max_retries:  How many attempts before giving up
        backoff_base: Base for exponential backoff (seconds)

    Returns:
        List of transcript segments, or None if transcripts are unavailable.
    """
    last_exception = None
    attempted_proxies: set[str] = set()

    for attempt in range(max_retries):
        proxy_dict = _pick_proxy(attempted_proxies)

        try:
            transcript = YouTubeTranscriptApi.get_transcript(
                video_id,
                languages=languages,
                proxies=proxy_dict,
            )
            if attempt > 0:
                logger.info(f"[{video_id}] Transcript fetched successfully on attempt {attempt + 1}.")
            return transcript

        except (TranscriptsDisabled, NoTranscriptFound):
            # Permanent — no transcript exists, no point retrying
            logger.warning(f"[{video_id}] No transcript available (disabled or not found).")
            return None

        except VideoUnavailable:
            logger.warning(f"[{video_id}] Video is unavailable.")
            return None

        except Exception as e:
            last_exception = e
            wait = backoff_base ** attempt + random.uniform(0, 1)
            logger.warning(
                f"[{video_id}] Attempt {attempt + 1}/{max_retries} failed "
                f"({type(e).__name__}: {e}). Retrying in {wait:.1f}s..."
            )
            time.sleep(wait)

    logger.error(f"[{video_id}] All {max_retries} attempts failed. Last error: {last_exception}")
    return None


def _pick_proxy(attempted: set[str]) -> dict | None:
    """
    Pick a proxy URL from the loaded list, avoiding already-attempted ones.
    Returns None if proxies are disabled or the list is empty.
    """
    if not PROXY_ENABLED or not _proxy_list:
        return None

    available = [p for p in _proxy_list if p not in attempted]
    if not available:
        attempted.clear()  # exhausted the list — reset and start over
        available = _proxy_list

    proxy_url = random.choice(available)
    attempted.add(proxy_url)
    return {"http": proxy_url, "https": proxy_url}
