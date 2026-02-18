"""
seeder.py — Startup backfill
─────────────────────────────────────────────────────────────────────────────
On every container start, fetch the 3 most recent RSS entries for every
configured channel and process any that don't already exist as Trello cards
anywhere on the board (any list).

Duplicate guard
───────────────
Only the TRELLO board state is used to decide whether to skip a video.
The local DB is intentionally excluded from this check because a video can
be recorded in the DB without a corresponding Trello card existing (e.g. from
a failed previous run, or a card that was subsequently deleted from Trello).

  Trello card active   → skip   (card already visible on board)
  Trello card archived → skip   (card exists, just archived)
  Trello card deleted  → seed   (card gone from API — re-create it) ✓
  Only in DB, no card  → seed   (DB record doesn't mean card exists) ✓

Videos are still written to the DB after seeding so the normal polling loop
never re-processes them.
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
    Trello board (active + archived lists/cards; excludes deleted cards).

    Falls back to an empty set if the backend isn't Trello or the call fails,
    so seeding still works — it'll just risk duplicates in edge cases.
    """
    try:
        return backend.get_existing_video_ids()
    except AttributeError:
        logger.debug("Seeder: backend has no get_existing_video_ids(); skipping Trello dedup")
        return set()
    except Exception as exc:
        logger.warning("Seeder: could not fetch existing Trello cards — %s", exc)
        return set()


# ─────────────────────────────────────────────────────────────────────────────
#  Core seed routine
# ─────────────────────────────────────────────────────────────────────────────

def run_seed(backend) -> None:
    """
    Called once at startup.  For every configured channel, fetch the
    SEED_DEPTH most recent videos and create a Trello card for any that
    aren't already on the board (active or archived).
    """
    if not config.CHANNELS:
        logger.info("Seeder: no channels configured — skipping")
        return

    logger.info(
        "Seeder: scanning %d channel(s) for up to %d recent video(s) each …",
        len(config.CHANNELS),
        SEED_DEPTH,
    )

    # Duplicate guard is Trello-only: a DB record does not mean a card exists.
    # (Videos may be in the DB from a previous run where the card was later
    # deleted, or from a run where card creation failed after DB write.)
    trello_ids: Set[str] = _get_trello_video_ids(backend)
    logger.debug("Seeder: %d video ID(s) already on Trello board", len(trello_ids))

    # Track what we process this run to avoid intra-run duplicates
    seen_this_run: Set[str] = set()

    seeded = 0
    skipped = 0

    for channel_id, channel_name in config.CHANNELS.items():
        entries = _fetch_recent_entries(channel_id, SEED_DEPTH)
        logger.debug(
            "Seeder: %s — %d RSS entry(ies) fetched", channel_name, len(entries)
        )

        for entry in entries:
            video_id = entry["video_id"]

            if video_id in trello_ids or video_id in seen_this_run:
                logger.debug(
                    "Seeder: skipping %s (%s) — card already exists on Trello",
                    video_id,
                    entry["title"],
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
                seen_this_run.add(video_id)
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
            time.sleep(10)

    logger.info(
        "Seeder: complete — %d video(s) seeded, %d skipped (card already on Trello)",
        seeded,
        skipped,
    )


def _process_seed_entry(entry: dict, channel_name: str, backend) -> None:
    """Fetch transcript → summarize → publish → record in DB."""
    video_id = entry["video_id"]

    # 1. Transcript (3-tier fallback — same as normal poll pipeline)
    # Returns a TranscriptResult dataclass, not a dict
    transcript = transcripts.get_transcript(video_id, entry["title"])

    # 2. Summarize — kwarg is `transcript`, value is the TranscriptResult object
    summary_result = summarizer.summarize(
        video_id=video_id,
        title=entry["title"],
        channel_name=channel_name,
        transcript=transcript,
    )

    # 3. Publish to output backend — .tier is an int attribute, .text via summary_result
    backend.publish(
        video_id=video_id,
        title=entry["title"],
        channel_name=channel_name,
        url=entry["url"],
        summary=summary_result.text,
        transcript_tier=transcript.tier,
    )

    # 4. Record in local DB so the polling loop never re-processes it
    database.mark_video_seen(
        video_id=video_id,
        channel_id=entry["channel_id"],
        channel_name=channel_name,
        title=entry["title"],
        summary=summary_result.text,
        transcript_tier=transcript.tier,
    )