"""
output/base.py — Abstract base class for output backends.

Implement this interface to add new output destinations.
Current implementations:
  - TrelloBackend   (output/trello_backend.py)
  - DashboardBackend (output/dashboard_backend.py — future)

The monitor.py orchestrator calls backend.publish() without knowing
which backend is active, keeping the pipeline output-agnostic.
"""

import sqlite3
from abc import ABC, abstractmethod


class OutputBackend(ABC):
    """
    Abstract base class all output backends must implement.

    Each backend receives a fully processed video row from the database
    and is responsible for delivering it to its destination.
    """

    @abstractmethod
    def publish(self, video: sqlite3.Row) -> str:
        """
        Publish a completed video summary to the output destination.

        Args:
            video: A database Row with fields matching the `videos` table schema.
                   Relevant fields: video_id, channel_name, title, summary_text,
                   published_at, transcript_tier.

        Returns:
            A string reference ID (e.g. Trello card ID, dashboard row ID)
            that is stored in the database for audit/deduplication.

        Raises:
            Exception: Any exception will be caught by the caller and
                       recorded as output_status='failed'.
        """

    def validate_config(self) -> None:
        """
        Optional: validate that required config/credentials are present.
        Called at startup before the poll loop begins.
        Raise ValueError with a descriptive message if something is missing.
        """
