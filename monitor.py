"""
monitor.py — Main orchestration loop for yt-monitor.

Startup sequence:
  1. Validate config and credentials
  2. Initialize the database
  3. Enter the polling loop:
     a. Poll all channels for new videos (RSS or API)
     b. Fetch transcripts for new videos (3-tier fallback)
     c. Summarize with Claude API
     d. Publish to the configured output backend
     e. Sleep for POLL_INTERVAL_SECONDS
  4. Repeat forever (Ctrl-C to stop; Docker SIGTERM handled gracefully)
"""

import logging
import signal
import sys
import time

import config
import database
import channels as channel_module
import transcripts
from transcripts import init_proxies
import summarizer
from output import get_backend
import seeder

# ── Logging setup ────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format=config.LOG_FORMAT,
    datefmt=config.LOG_DATE_FORMAT,
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger(__name__)

# ── Graceful shutdown ────────────────────────────────────────────────────────
_shutdown_requested = False


def _handle_sigterm(signum, frame):
    global _shutdown_requested
    logger.info("Received SIGTERM — finishing current job then shutting down.")
    _shutdown_requested = True


signal.signal(signal.SIGTERM, _handle_sigterm)


# ────────────────────────────────────────────────────────────
#  Startup validation
# ────────────────────────────────────────────────────────────

def _validate_startup(backend) -> None:
    """Check all required config is present before entering the poll loop."""
    errors = []

    if not config.ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY is not set in .env")

    if not config.CHANNELS:
        errors.append(
            "No channels configured. Add channel IDs to CHANNELS dict in config.py."
        )

    try:
        backend.validate_config()
    except ValueError as exc:
        errors.append(str(exc))

    if config.YOUTUBE_API_ENABLED and not config.YOUTUBE_API_KEY:
        errors.append(
            "YOUTUBE_API_ENABLED=True but YOUTUBE_API_KEY is not set in .env."
        )

    if config.PROXY_ENABLED and (not config.PROXY_USERNAME or not config.PROXY_PASSWORD):
        errors.append("PROXY_ENABLED=True but PROXY_USERNAME or PROXY_PASSWORD is not set in .env.")

    if errors:
        for err in errors:
            logger.error("Config error: %s", err)
        sys.exit(1)


# ────────────────────────────────────────────────────────────
#  Per-video processing
# ────────────────────────────────────────────────────────────

def process_video(video_entry, backend) -> None:
    """
    Full processing pipeline for a single newly discovered video:
      transcript → summarize → publish
    """
    vid = video_entry.video_id
    logger.info("Processing: [%s] %s", video_entry.channel_name, video_entry.title)

    # ── Step 1: Transcript ───────────────────────────────────
    try:
        transcript = transcripts.get_transcript(
            video_id=vid,
            title=video_entry.title,
            description=video_entry.description,
        )
        database.update_transcript(
            video_id=vid,
            tier=transcript.tier,
            transcript_text=transcript.text,
        )
    except Exception as exc:
        logger.error("Transcript extraction failed for %s: %s", vid, exc)
        database.update_summary(
            video_id=vid,
            status="failed",
            error=f"Transcript error: {exc}",
        )
        return

    # ── Step 2: Summarize ────────────────────────────────────
    database.update_summary(vid, status="processing")
    try:
        result = summarizer.summarize(
            video_id=vid,
            channel_name=video_entry.channel_name,
            title=video_entry.title,
            transcript=transcript,
        )
        database.update_summary(
            video_id=vid,
            status="done",
            summary_text=result.text,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
        )
    except Exception as exc:
        logger.error("Summarization failed for %s: %s", vid, exc)
        database.update_summary(
            video_id=vid,
            status="failed",
            error=str(exc),
        )
        return

    # ── Step 3: Publish ──────────────────────────────────────
    pending_rows = database.get_pending_output_videos()
    for row in pending_rows:
        if row["video_id"] != vid:
            continue
        try:
            ref = backend.publish(row)
            database.update_output(vid, status="done", output_ref=ref)
        except Exception as exc:
            logger.error("Output publish failed for %s: %s", vid, exc)
            database.update_output(vid, status="failed", error=str(exc))


# ────────────────────────────────────────────────────────────
#  Main poll loop
# ────────────────────────────────────────────────────────────

def run_once(backend) -> None:
    """Execute one full poll-and-process cycle."""
    logger.info("── Poll cycle starting ──────────────────────────────────")
    new_videos = channel_module.poll_all_channels()

    if not new_videos:
        logger.info("No new videos found this cycle.")
        return

    for video_entry in new_videos:
        if _shutdown_requested:
            logger.info("Shutdown requested — stopping mid-cycle.")
            break
        process_video(video_entry, backend)

    logger.info(
        "── Poll cycle complete — processed %d video(s) ─────────────────",
        len(new_videos)
    )


def main() -> None:
    logger.info("=" * 60)
    logger.info(" yt-monitor starting up")
    logger.info(" Model:    %s", config.CLAUDE_MODEL)
    logger.info(" Backend:  %s", config.OUTPUT_BACKEND)
    logger.info(" Channels: %d configured", len(config.CHANNELS))
    logger.info(" Poll interval: %ds", config.POLL_INTERVAL_SECONDS)
    logger.info(" Proxy:    %s", "enabled" if config.PROXY_ENABLED else "disabled")
    logger.info(" YouTube API: %s", "enabled" if config.YOUTUBE_API_ENABLED else "disabled")
    logger.info(" Batch API: %s", "enabled" if config.BATCH_API_ENABLED else "disabled")
    logger.info(" Whisper:  %s", "enabled" if config.WHISPER_ENABLED else "disabled")
    logger.info("=" * 60)

    # Initialise DB schema
    database.migrate()

    # Initialize proxy list (no-op if PROXY_ENABLED = False)
    init_proxies()

    # Initialize and validate the output backend
    backend = get_backend()
    _validate_startup(backend)
    logger.info("Startup validation passed")

    # ── Seed initial data ─────────────────────────────────────────────────
    logger.info("Running startup seed …")
    try:
        seeder.run_seed(backend)
    except Exception as exc:
        logger.error("Startup seed failed — continuing anyway: %s", exc, exc_info=True)

    logger.info("Entering poll loop")

    # Run immediately on first start, then sleep between cycles
    while not _shutdown_requested:
        try:
            run_once(backend)
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down.")
            break
        except Exception as exc:
            logger.error("Unhandled error in poll cycle: %s", exc, exc_info=True)
            # Don't crash the whole process on a single bad cycle
            logger.info("Continuing after error — next poll in %ds", config.POLL_INTERVAL_SECONDS)

        if _shutdown_requested:
            break

        logger.info("Sleeping %d seconds until next poll…", config.POLL_INTERVAL_SECONDS)
        # Sleep in short increments so SIGTERM is handled promptly
        for _ in range(config.POLL_INTERVAL_SECONDS):
            if _shutdown_requested:
                break
            time.sleep(1)

    logger.info("yt-monitor shut down cleanly.")


if __name__ == "__main__":
    main()
