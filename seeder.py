"""
seeder.py — Startup backfill
─────────────────────────────────────────────────────────────────────────────
On every container start, fetch the 3 most recent RSS entries for every
configured channel and process any that don't already exist as Trello cards
anywhere on the board (any list).

This populates initial data so the board has content immediately, and makes
it easy to demo the project without waiting for new uploads.

Duplicate guard
───────────────
We search every card on the board (not just the target list) for the YouTube
video ID embedded in the card description.  If a card already references that
video ID, we skip it — regardless of which list it's in.

The processed videos are also recorded in the local SQLite database so the
normal polling loop never re-processes them.
"""

import logging
import re
import time
from typing import Set

import feedparser

import config
import database
import transcripts
import summarizer

logger = logging.getLogger(__name__)

# How many recent videos to seed per channel
SEED_DEPTH = 3

# Regex to pull a YouTube video ID out of any URL in a Trello card
_YT_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)"
    r"([A-Za-z0-9_-]{11})"
)


# ─────────────────────────────────────────────────────────────────────────────
#  RSS helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_recent_entries(channel_id: str, depth: int = SEED_DEPTH) -> list[dict]:
    """Return up to *depth* most-recent RSS entries for *channel_id*."""
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        feed = feedparser.parse(url)
        entries = feed.entries[:depth]
        result = []
        for entry in entries:
            video_id = entry.get("yt_videoid") or entry.get("id", "").split(":")[-1]
            if not video_id:
                continue
            result.append({
                "video_id": video_id,
                "title": entry.get("title", "Untitled"),
                "url": f"https://www.youtube.com/watch?v={video_id}",
                "published": entry.get("published", ""),
                "channel_id": channel_id,
            })
        return result
    except Exception as exc:
        logger.warning("Seeder: RSS fetch failed for %s — %s", channel_id, exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
#  Trello duplicate detection
# ─────────────────────────────────────────────────────────────────────────────

def _get_trello_video_ids(backend) -> Set[str]:
    """
    Return the set of YouTube video IDs already present anywhere on the
    Trello board (across all lists).

    Falls back to an empty set if the backend isn't Trello or the call fails,
    so seeding still works — it'll just risk duplicates in edge cases.
    """
    try:
        return backend.get_existing_video_ids()
    except AttributeError:
        # Non-Trello backend — no dedup needed at the Trello level
        logger.debug("Seeder: backend has no get_existing_video_ids(); skipping Trello dedup")
        return set()
    except Exception as exc:
        logger.warning("Seeder: could not fetch existing Trello cards — %s", exc)
        return set()


# ─────────────────────────────────────────────────────────────────────────────
#  Database duplicate detection
# ─────────────────────────────────────────────────────────────────────────────

def _get_db_video_ids() -> Set[str]:
    """Return the set of video IDs already recorded in the local database."""
    try:
        rows = database.get_all_video_ids()
        return set(rows)
    except Exception as exc:
        logger.warning("Seeder: could not query local DB for existing IDs — %s", exc)
        return set()


# ─────────────────────────────────────────────────────────────────────────────
#  Core seed routine
# ─────────────────────────────────────────────────────────────────────────────

def run_seed(backend) -> None:
    """
    Called once at startup.  For every configured channel, fetch the
    SEED_DEPTH most recent videos and process any that aren't already
    represented in Trello or the local database.
    """
    if not config.CHANNELS:
        logger.info("Seeder: no channels configured — skipping")
        return

    logger.info(
        "Seeder: scanning %d channel(s) for up to %d recent video(s) each …",
        len(config.CHANNELS),
        SEED_DEPTH,
    )

    # Build the full "already exists" set once — DB + Trello board
    existing_ids: Set[str] = _get_db_video_ids() | _get_trello_video_ids(backend)
    logger.debug("Seeder: %d video ID(s) already known", len(existing_ids))

    seeded = 0
    skipped = 0

    for channel_id, channel_name in config.CHANNELS.items():
        entries = _fetch_recent_entries(channel_id, SEED_DEPTH)
        logger.debug(
            "Seeder: %s — %d RSS entry(ies) fetched", channel_name, len(entries)
        )

        for entry in entries:
            video_id = entry["video_id"]

            if video_id in existing_ids:
                logger.debug(
                    "Seeder: skipping %s (%s) — already exists", video_id, entry["title"]
                )
                skipped += 1
                continue

            logger.info(
                "Seeder: processing %s — %s [%s]",
                channel_name,
                entry["title"],
                video_id,
            )

            try:
                _process_seed_entry(entry, channel_name, backend)
                existing_ids.add(video_id)   # prevent intra-run duplicates
                seeded += 1
            except Exception as exc:
                logger.error(
                    "Seeder: failed to process %s (%s) — %s",
                    video_id,
                    entry["title"],
                    exc,
                    exc_info=True,
                )

            # Small courtesy delay between API calls
            time.sleep(1)

    logger.info(
        "Seeder: complete — %d video(s) seeded, %d skipped (already existed)",
        seeded,
        skipped,
    )


def _process_seed_entry(entry: dict, channel_name: str, backend) -> None:
    """Fetch transcript → summarize → publish → record in DB."""
    video_id = entry["video_id"]

    # 1. Transcript (3-tier fallback — same as normal poll pipeline)
    transcript_data = transcripts.get_transcript(video_id, entry["title"])

    # 2. Summarize
    summary = summarizer.summarize(
        video_id=video_id,
        title=entry["title"],
        channel_name=channel_name,
        transcript_data=transcript_data,
    )

    # 3. Publish to output backend
    backend.publish(
        video_id=video_id,
        title=entry["title"],
        channel_name=channel_name,
        url=entry["url"],
        summary=summary,
        transcript_tier=transcript_data.get("tier", "unknown"),
    )

    # 4. Record in local DB so the polling loop never re-processes it
    database.mark_video_seen(
        video_id=video_id,
        channel_id=entry["channel_id"],
        channel_name=channel_name,  # ← add this
        title=entry["title"],
        url=entry["url"],
        summary=summary,
        transcript_tier=int(transcript_data.get("tier", 3)),
    )
