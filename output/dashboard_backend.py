"""
output/dashboard_backend.py — Flask dashboard output backend (future).

When OUTPUT_BACKEND="dashboard" in config.py, this backend is used instead
of Trello. It writes summary data to SQLite in a format the Flask dashboard
can read. Since both the monitor and dashboard share the same SQLite database,
"publishing" here is essentially a no-op — the data is already in the DB.

This stub exists so the pluggable backend pattern works correctly and you
can flip OUTPUT_BACKEND to "dashboard" as soon as the Flask app is built.
"""

import logging
import sqlite3

from output.base import OutputBackend

logger = logging.getLogger(__name__)


class DashboardBackend(OutputBackend):
    """
    Publishes summaries to the local SQLite database for the Flask dashboard.

    The video row already exists in the database when publish() is called;
    this backend simply marks it as delivered and returns a reference ID
    consistent with the interface.

    ⚠️  The Flask dashboard (dashboard/app.py) is not yet implemented.
    This backend is ready to use once the dashboard is built.
    """

    def validate_config(self) -> None:
        # No external credentials needed — everything is local SQLite
        logger.info(
            "Dashboard backend active. "
            "Summaries will be stored in SQLite and served by the Flask app."
        )

    def publish(self, video: sqlite3.Row) -> str:
        """
        The summary is already in the database. Just return the video_id
        as the reference so the output_status can be set to 'done'.
        """
        logger.info(
            "Dashboard: summary for %s stored in DB (ready for dashboard).",
            video["video_id"]
        )
        return video["video_id"]
