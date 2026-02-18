"""
transcripts.py — Three-tier transcript extraction for yt-monitor.

Tier 1: youtube-transcript-api (auto-generated or manual captions, no auth)
Tier 2: Whisper audio transcription via yt-dlp (enabled by WHISPER_ENABLED)
Tier 3: Metadata-only fallback (title + description from RSS feed)

Each tier is attempted in order. The first successful result is returned
along with the tier number so the summarizer can adjust its prompt.
"""

import logging
import time
from dataclasses import dataclass
from typing import Optional

import config

logger = logging.getLogger(__name__)


@dataclass
class TranscriptResult:
    tier: int           # 1, 2, or 3
    text: str
    language: Optional[str] = None
    is_auto_generated: bool = False


# ────────────────────────────────────────────────────────────
#  Tier 1 — youtube-transcript-api
# ────────────────────────────────────────────────────────────

def _fetch_tier1(video_id: str) -> Optional[TranscriptResult]:
    """
    Attempt to retrieve captions via youtube-transcript-api.

    No API key required. Supports auto-generated and manual captions.
    Will raise RequestBlocked if running on a datacenter/VPS IP without
    a residential proxy — enable PROXY_ENABLED in config.py if needed.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        from youtube_transcript_api._errors import (
            TranscriptsDisabled,
            NoTranscriptFound,
            VideoUnavailable,
        )
        from youtube_transcript_api.proxies import WebshareProxyConfig

        # Build the API object — reuse across calls in a single process run
        if config.PROXY_ENABLED:
            if not config.PROXY_USERNAME or not config.PROXY_PASSWORD:
                logger.warning(
                    "PROXY_ENABLED=True but PROXY_USERNAME/PASSWORD not set in .env. "
                    "Falling back to direct connection."
                )
                ytt = YouTubeTranscriptApi()
            else:
                logger.debug("Using Webshare proxy for transcript request")
                ytt = YouTubeTranscriptApi(
                    proxy_config=WebshareProxyConfig(
                        proxy_username=config.PROXY_USERNAME,
                        proxy_password=config.PROXY_PASSWORD,
                    )
                )
        else:
            ytt = YouTubeTranscriptApi()

        transcript_list = ytt.list(video_id)

        # Try to find a manual English transcript first, then auto-generated
        transcript = None
        language = None
        is_auto = False

        try:
            transcript = transcript_list.find_manually_created_transcript(["en", "en-US", "en-GB"])
            language = transcript.language_code
            is_auto = False
        except Exception:
            pass

        if transcript is None:
            try:
                transcript = transcript_list.find_generated_transcript(["en", "en-US", "en-GB"])
                language = transcript.language_code
                is_auto = True
            except Exception:
                pass

        if transcript is None:
            # Try the first available language as last resort
            try:
                available = list(transcript_list)
                if available:
                    transcript = available[0]
                    language = transcript.language_code
                    is_auto = transcript.is_generated
            except Exception:
                pass

        if transcript is None:
            logger.info("No transcript found for %s via Tier 1", video_id)
            return None

        fetched = transcript.fetch()
        # Join snippets into plain text, preserving natural spacing
        text = " ".join(snippet.text.strip() for snippet in fetched if snippet.text.strip())
        text = text[:config.TRANSCRIPT_MAX_CHARS]

        logger.info(
            "Tier 1 transcript for %s (%s, %s, %d chars)",
            video_id,
            language,
            "auto" if is_auto else "manual",
            len(text),
        )
        return TranscriptResult(
            tier=1,
            text=text,
            language=language,
            is_auto_generated=is_auto,
        )

    except Exception as exc:
        # Catch-all: any unexpected error from the library
        err_name = type(exc).__name__
        logger.warning("Tier 1 failed for %s (%s: %s)", video_id, err_name, exc)
        return None


# ────────────────────────────────────────────────────────────
#  Tier 2 — Whisper audio transcription (optional)
# ────────────────────────────────────────────────────────────

def _fetch_tier2(video_id: str) -> Optional[TranscriptResult]:
    """
    Download audio via yt-dlp and transcribe with Whisper.
    Only attempted when WHISPER_ENABLED=True in config.py.

    Backend options (set WHISPER_BACKEND in config.py):
      "local" — openai-whisper on this machine (needs ffmpeg + GPU recommended)
      "api"   — OpenAI Whisper API ($0.006/min, needs OPENAI_API_KEY)
    """
    if not config.WHISPER_ENABLED:
        return None

    logger.info("Attempting Tier 2 (Whisper) for %s", video_id)

    try:
        import tempfile
        import os

        # ── Download audio with yt-dlp ───────────────────────────
        try:
            import yt_dlp  # noqa: F401
        except ImportError:
            logger.error(
                "yt-dlp is not installed. "
                "Add 'yt-dlp' to requirements.txt and rebuild the Docker image."
            )
            return None

        with tempfile.TemporaryDirectory() as tmpdir:
            audio_path = os.path.join(tmpdir, f"{video_id}.mp3")
            ydl_opts = {
                "format": "bestaudio/best",
                "outtmpl": audio_path,
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "64",  # Low quality is fine for speech
                }],
                "quiet": True,
                "no_warnings": True,
            }

            with __import__("yt_dlp").YoutubeDL(ydl_opts) as ydl:
                ydl.download([f"https://www.youtube.com/watch?v={video_id}"])

            # The actual output file may have .mp3 appended differently
            candidates = [
                audio_path,
                audio_path + ".mp3",
                os.path.join(tmpdir, video_id + ".mp3"),
            ]
            actual_path = next((p for p in candidates if os.path.exists(p)), None)

            if actual_path is None:
                logger.error("yt-dlp did not produce an output file for %s", video_id)
                return None

            # ── Transcribe ──────────────────────────────────────────
            if config.WHISPER_BACKEND == "api":
                text = _whisper_api(actual_path)
            else:
                text = _whisper_local(actual_path)

        if not text:
            return None

        text = text[:config.TRANSCRIPT_MAX_CHARS]
        logger.info("Tier 2 transcript for %s (%d chars)", video_id, len(text))
        return TranscriptResult(tier=2, text=text, is_auto_generated=True)

    except Exception as exc:
        logger.warning("Tier 2 failed for %s: %s", video_id, exc)
        return None


def _whisper_api(audio_path: str) -> Optional[str]:
    """Transcribe using OpenAI's hosted Whisper API."""
    import os
    import openai  # noqa: F401 — only imported when WHISPER_ENABLED=True

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("WHISPER_BACKEND='api' but OPENAI_API_KEY is not set.")
        return None

    client = __import__("openai").OpenAI(api_key=api_key)
    with open(audio_path, "rb") as f:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            response_format="text",
        )
    return response if isinstance(response, str) else getattr(response, "text", None)


def _whisper_local(audio_path: str) -> Optional[str]:
    """Transcribe using locally installed openai-whisper."""
    try:
        import whisper  # noqa: F401
    except ImportError:
        logger.error(
            "openai-whisper is not installed. "
            "Add 'openai-whisper' to requirements.txt and rebuild."
        )
        return None

    model = __import__("whisper").load_model("turbo")
    result = model.transcribe(audio_path)
    return result.get("text")


# ────────────────────────────────────────────────────────────
#  Tier 3 — Metadata-only fallback
# ────────────────────────────────────────────────────────────

def _fetch_tier3(video_id: str, title: str, description: str) -> TranscriptResult:
    """
    Construct a minimal text from title and description.
    Claude will be told this is metadata-only so it adjusts its summary.
    """
    text = f"Video Title: {title}\n\nDescription:\n{description or '(no description available)'}"
    logger.info("Using Tier 3 (metadata-only) for %s", video_id)
    return TranscriptResult(tier=3, text=text)


# ────────────────────────────────────────────────────────────
#  Public interface
# ────────────────────────────────────────────────────────────

def get_transcript(
    video_id: str,
    title: str = "",
    description: str = "",
) -> TranscriptResult:
    """
    Attempt transcript extraction through all three tiers in order.
    Always returns a TranscriptResult — Tier 3 is the guaranteed fallback.

    Args:
        video_id:    YouTube video ID (11-char string)
        title:       Video title (used for Tier 3 fallback)
        description: Video description from RSS feed (used for Tier 3 fallback)
    """
    # Brief delay to avoid hammering YouTube's endpoints
    time.sleep(config.TRANSCRIPT_REQUEST_DELAY_SECONDS)

    # Tier 1: captions
    result = _fetch_tier1(video_id)
    if result:
        return result

    # Tier 2: Whisper (only if enabled)
    result = _fetch_tier2(video_id)
    if result:
        return result

    # Tier 3: guaranteed fallback
    return _fetch_tier3(video_id, title, description)
