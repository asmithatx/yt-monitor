"""
config.py — Central configuration and feature flags for yt-monitor.

All tuneable settings live here. Feature flags let you opt-in to
capabilities without touching application code. Secrets are loaded
from the .env file via python-dotenv.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (works both locally and in Docker)
load_dotenv(Path(__file__).parent / ".env")


# ════════════════════════════════════════════════════════════
#  FEATURE FLAGS
#  Change these to enable/disable capabilities.
# ════════════════════════════════════════════════════════════

# Output backend: "trello" or "dashboard"
# "dashboard" requires the Flask app to be running (see dashboard/app.py)
OUTPUT_BACKEND: str = "trello"

# Set True to enable YouTube Data API enrichment after RSS detection.
# Provides richer metadata (duration, tags, exact view count) but
# requires YOUTUBE_API_KEY in .env.
YOUTUBE_API_ENABLED: bool = False

# Set True to route youtube-transcript-api requests through a residential
# proxy. Recommended when running on cloud/VPS IPs. Requires PROXY_USERNAME
# and PROXY_PASSWORD in .env. Has no effect when False.
PROXY_ENABLED: bool = False

# Set True to use Anthropic's Message Batches API instead of real-time
# calls. Gives a 50% cost discount; results arrive within 24 hours.
# Not suitable if you want summaries immediately. Has no effect when False.
BATCH_API_ENABLED: bool = False

# Set True to enable Whisper audio transcription as Tier-2 fallback.
# Requires yt-dlp and ffmpeg. When False, falls through to Tier-3
# (metadata-only summary) if captions are unavailable.
WHISPER_ENABLED: bool = False

# Whisper backend to use when WHISPER_ENABLED=True.
# "local"  → runs openai-whisper on this machine (needs GPU for speed)
# "api"    → uses OpenAI's hosted Whisper API ($0.006/min, needs OPENAI_API_KEY)
WHISPER_BACKEND: str = "api"


# ════════════════════════════════════════════════════════════
#  CHANNELS TO MONITOR
#  Add YouTube channel IDs here. The channel ID starts with "UC".
#  Find it via: channel page → View Source → search "channelId"
#  or use https://commentpicker.com/youtube-channel-id.php
# ════════════════════════════════════════════════════════════

CHANNELS: dict[str, str] = {
    # "UCxxxxxxxxxxxxxxxxxxxxxxxx": "Friendly Channel Name",
    # Example (replace with real channels):
    # "UCBcRF18a7Qf58cCRy5xuWwQ": "Michael Reeves",
    "UCv9ztg_Qfj6_kmtWoFWyHYw": "Andrew Smith's YT channel (for testing)",
}


# ════════════════════════════════════════════════════════════
#  POLLING SETTINGS
# ════════════════════════════════════════════════════════════

# How often to check all channels for new videos (seconds).
# Default: 1800 = 30 minutes. YouTube RSS feeds update within ~60 min
# of a new upload, so polling more frequently than 15 min is wasteful.
POLL_INTERVAL_SECONDS: int = 1800

# Seconds to wait between transcript requests. Helps avoid rate-limiting
# from YouTube when processing multiple new videos in one run.
TRANSCRIPT_REQUEST_DELAY_SECONDS: float = 2.0

# Maximum age (in days) of a video to process on first run.
# Prevents summarizing an entire channel backlog when first added.
# Set to 0 to disable the limit (process all 15 feed entries on first run).
MAX_VIDEO_AGE_DAYS: int = 3


# ════════════════════════════════════════════════════════════
#  CLAUDE / ANTHROPIC
# ════════════════════════════════════════════════════════════

ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

# Maximum tokens for the generated summary.
SUMMARY_MAX_TOKENS: int = 1024

# Transcript character limit sent to Claude. At ~4 chars/token,
# 400,000 chars ≈ 100K tokens — fits comfortably in the context window.
# Transcripts longer than this are truncated with a note appended.
TRANSCRIPT_MAX_CHARS: int = 400_000


# ════════════════════════════════════════════════════════════
#  TRELLO
# ════════════════════════════════════════════════════════════

TRELLO_API_KEY: str = os.getenv("TRELLO_API_KEY", "")
TRELLO_TOKEN: str = os.getenv("TRELLO_TOKEN", "")
TRELLO_LIST_ID: str = os.getenv("TRELLO_LIST_ID", "")
TRELLO_LABEL_IDS: list[str] = [
    lbl.strip()
    for lbl in os.getenv("TRELLO_LABEL_IDS", "").split(",")
    if lbl.strip()
]


# ════════════════════════════════════════════════════════════
#  YOUTUBE DATA API (only used when YOUTUBE_API_ENABLED=True)
# ════════════════════════════════════════════════════════════

YOUTUBE_API_KEY: str = os.getenv("YOUTUBE_API_KEY", "")


# ════════════════════════════════════════════════════════════
#  PROXY (only used when PROXY_ENABLED=True)
# ════════════════════════════════════════════════════════════

PROXY_USERNAME: str = os.getenv("PROXY_USERNAME", "")
PROXY_PASSWORD: str = os.getenv("PROXY_PASSWORD", "")


# ════════════════════════════════════════════════════════════
#  DATABASE
# ════════════════════════════════════════════════════════════

DATABASE_PATH: str = os.getenv("DATABASE_PATH", "data/monitor.db")


# ════════════════════════════════════════════════════════════
#  LOGGING
# ════════════════════════════════════════════════════════════

LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")
LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"
