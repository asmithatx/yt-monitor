"""
output/__init__.py — Backend factory for yt-monitor.

Usage:
    from output import get_backend
    backend = get_backend()
    backend.publish(video_row)
"""

from output.base import OutputBackend


def get_backend() -> OutputBackend:
    """
    Return the configured output backend instance.
    Add new backends here as elif branches.
    """
    import config

    backend_name = config.OUTPUT_BACKEND.lower().strip()

    if backend_name == "trello":
        from output.trello_backend import TrelloBackend
        return TrelloBackend()

    elif backend_name == "dashboard":
        from output.dashboard_backend import DashboardBackend
        return DashboardBackend()

    else:
        raise ValueError(
            f"Unknown OUTPUT_BACKEND='{config.OUTPUT_BACKEND}' in config.py. "
            "Valid options: 'trello', 'dashboard'"
        )
