"""
dashboard/app.py — Flask web dashboard for yt-monitor (stub).

This is the foundation for the custom web frontend. Currently provides
a read-only view of the summaries stored in SQLite by the monitor.

To enable:
  1. Set OUTPUT_BACKEND="dashboard" in config.py
  2. Run this app alongside monitor.py (both share the same SQLite DB)
  3. Visit http://localhost:5000

TODO (future development):
  - Channel filtering
  - Full-text search
  - Mark as read / archive
  - Trend analytics
  - Mobile-responsive layout
"""

import sys
import os

# Ensure the project root is on the path when running this file directly
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from flask import Flask, render_template, jsonify, abort
import database

app = Flask(__name__)


@app.route("/")
def index():
    """Main dashboard — list of recent video summaries."""
    videos = database.get_recent_summaries(limit=50)
    return render_template("index.html", videos=videos)


@app.route("/video/<video_id>")
def video_detail(video_id: str):
    """Detail view for a single video summary."""
    import sqlite3
    import config
    db_path = config.DATABASE_PATH
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM videos WHERE video_id = ?", (video_id,)
    ).fetchone()
    conn.close()

    if row is None:
        abort(404)
    return render_template("detail.html", video=row)


@app.route("/api/summaries")
def api_summaries():
    """JSON endpoint — useful for future integrations."""
    videos = database.get_recent_summaries(limit=100)
    return jsonify([dict(v) for v in videos])


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
