"""
output/trello_backend.py
─────────────────────────────────────────────────────────────────────────────
Changes vs previous version:
  • get_existing_video_ids() now uses filter=all so archived cards are
    included in the duplicate check.  Hard-deleted cards are never returned
    by the Trello API regardless of filter, so they will be re-created by
    the seeder — which is the desired behaviour.

Duplicate behaviour summary:
  active card   → returned by filter=all → seeder skips it  (no duplicate)
  archived card → returned by filter=all → seeder skips it  (no duplicate)
  deleted card  → absent from API        → seeder creates it ✓
"""

import logging
import re
import requests

import config

logger = logging.getLogger(__name__)

# YouTube video ID regex — matches URLs embedded in card descriptions
_YT_ID_RE = re.compile(
    r"(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/embed/)"
    r"([A-Za-z0-9_-]{11})"
)

_BASE = "https://api.trello.com/1"


class TrelloBackend:
    """Publishes video summaries as Trello cards."""

    # ── Config validation ────────────────────────────────────────────────────

    def validate_config(self) -> None:
        missing = [
            k for k in ("TRELLO_API_KEY", "TRELLO_TOKEN", "TRELLO_LIST_ID")
            if not getattr(config, k, None)
        ]
        if missing:
            raise ValueError(
                f"Trello backend: missing required config: {', '.join(missing)}"
            )

    # ── Auth helpers ─────────────────────────────────────────────────────────

    @property
    def _auth(self) -> dict:
        return {"key": config.TRELLO_API_KEY, "token": config.TRELLO_TOKEN}

    def _get(self, path: str, **params) -> dict | list:
        resp = requests.get(
            f"{_BASE}/{path.lstrip('/')}",
            params={**self._auth, **params},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    def _post(self, path: str, **data) -> dict:
        resp = requests.post(
            f"{_BASE}/{path.lstrip('/')}",
            params=self._auth,
            json=data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # ── Board-wide duplicate detection (used by seeder.py) ───────────────────

    def get_existing_video_ids(self) -> set[str]:
        """
        Return the set of YouTube video IDs referenced by ANY card on the
        board that still exists — active or archived.

        Cards that were hard-deleted are absent from the Trello API entirely
        (regardless of filter), so they will NOT appear in this set, allowing
        the seeder to re-create them.

        Strategy:
          1. GET /lists/{list_id} to discover the board ID.
          2. GET /boards/{board_id}/cards?filter=all to fetch every card
             (active + archived; excludes deleted).
          3. Extract 11-char video IDs from YouTube URLs in name + desc.
        """
        # Step 1 — get the board ID from the configured list
        list_data = self._get(f"lists/{config.TRELLO_LIST_ID}", fields="idBoard")
        board_id = list_data["idBoard"]

        # Step 2 — fetch all cards on the board (lightweight fields only)
        # filter=all  → includes active cards AND archived (closed=true) cards
        #               but NOT hard-deleted cards (absent from the API entirely)
        # This means:
        #   active card   → found → skip (no duplicate)
        #   archived card → found → skip (no duplicate)
        #   deleted card  → not found → seed/create (desired behaviour)
        cards = self._get(
            f"boards/{board_id}/cards",
            filter="all",
            fields="name,desc",
        )

        # Step 3 — extract video IDs
        video_ids: set[str] = set()
        for card in cards:
            for field in (card.get("name", ""), card.get("desc", "")):
                for match in _YT_ID_RE.finditer(field):
                    video_ids.add(match.group(1))

        logger.debug(
            "Trello dedup: found %d video ID(s) across %d card(s) on board %s "
            "(active + archived, excludes deleted)",
            len(video_ids),
            len(cards),
            board_id,
        )
        return video_ids

    # ── Card creation ────────────────────────────────────────────────────────

    def publish(
        self,
        *,
        video_id: str,
        title: str,
        channel_name: str,
        url: str,
        summary: str,
        transcript_tier: str = "unknown",
    ) -> None:
        """Create a Trello card for the given video summary."""
        card_name = f"[{channel_name}] {title}"
        desc = self._format_description(
            url=url,
            summary=summary,
            transcript_tier=transcript_tier,
        )

        card = self._post(
            "cards",
            idList=config.TRELLO_LIST_ID,
            name=card_name,
            desc=desc,
            urlSource=url,
        )
        logger.info(
            "Trello: card created for %s — %s",
            video_id,
            card.get("shortUrl", ""),
        )

    @staticmethod
    def _format_description(*, url: str, summary: str, transcript_tier: str) -> str:
        tier_labels = {
            "1": "Full captions",
            "2": "Whisper transcription",
            "3": "Metadata only",
            "unknown": "Unknown",
        }
        tier_label = tier_labels.get(str(transcript_tier), str(transcript_tier))

        return (
            f"**Source:** {url}\n"
            f"**Transcript quality:** {tier_label}\n\n"
            f"---\n\n"
            f"{summary}"
        )
