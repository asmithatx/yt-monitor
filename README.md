# yt-monitor

A Docker-based YouTube channel monitor that automatically detects new videos,
extracts transcripts, and posts AI-generated summaries to Trello (or a custom
Flask dashboard).

## Features

- **RSS-based monitoring** — no YouTube API key required by default
- **3-tier transcript extraction**: captions → Whisper → metadata fallback
- **Claude AI summarization** with prompt caching (reduces token costs ~90%)
- **Trello integration** — one card per video, Markdown-formatted
- **Pluggable output backend** — swap Trello for a Flask dashboard by changing one config value
- **Docker-first** — runs on a laptop, workstation, or Raspberry Pi 4/5
- **Feature flags** for proxy support, YouTube API, Whisper, and batch API

---

## Quick Start

### 1. Prerequisites

- Docker + Docker Compose installed
- An [Anthropic API key](https://console.anthropic.com/)
- A Trello account (free tier is fine)

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in:
- `ANTHROPIC_API_KEY`
- Trello credentials (see below)

Edit `config.py` and add your channels to the `CHANNELS` dict:

```python
CHANNELS: dict[str, str] = {
    "UCxxxxxxxxxxxxxxxxxxxxxxxx": "Channel Name Here",
}
```

**Finding a YouTube Channel ID:**
- Go to the channel page in your browser
- View source (Ctrl+U) and search for `"channelId"`
- Or use https://commentpicker.com/youtube-channel-id.php

### 3. Get Trello credentials

1. Go to https://trello.com/power-ups/admin and create a new Power-Up
2. Copy your **API Key** → set as `TRELLO_API_KEY` in `.env`
3. Click "Token" link on that same page → authorise → copy the token → set as `TRELLO_TOKEN`
4. Find your **List ID**:
   - Open a Trello board in the browser
   - Add `.json` to the board URL: `https://trello.com/b/BOARD_ID/board-name.json`
   - Find the list you want and copy its `"id"` value → set as `TRELLO_LIST_ID`

### 4. Build and run

```bash
docker compose up --build -d
docker compose logs -f
```

The monitor will run immediately on startup, then poll every 30 minutes.

---

## Configuration Reference

All feature flags and settings are in `config.py`. Secrets go in `.env`.

| Setting | Default | Description |
|---|---|---|
| `OUTPUT_BACKEND` | `"trello"` | `"trello"` or `"dashboard"` |
| `CHANNELS` | `{}` | Dict of `{channel_id: name}` to monitor |
| `POLL_INTERVAL_SECONDS` | `1800` | How often to check for new videos (30 min) |
| `MAX_VIDEO_AGE_DAYS` | `3` | Skip videos older than this on first run |
| `YOUTUBE_API_ENABLED` | `False` | Use YouTube API for metadata enrichment |
| `PROXY_ENABLED` | `False` | Route transcript requests through a proxy |
| `WHISPER_ENABLED` | `False` | Enable Whisper as Tier-2 transcript fallback |
| `BATCH_API_ENABLED` | `False` | Use Anthropic Batch API (50% cheaper, async) |
| `CLAUDE_MODEL` | `claude-sonnet-4-5-...` | Override via `CLAUDE_MODEL` in `.env` |

---

## Enabling Optional Features

### Proxy support (for cloud/VPS deployments)

If YouTube blocks transcript requests from your server's IP:

1. Sign up for a residential proxy service (e.g. [Webshare](https://proxy.webshare.io))
2. Set `PROXY_USERNAME` and `PROXY_PASSWORD` in `.env`
3. Set `PROXY_ENABLED = True` in `config.py`
4. Rebuild: `docker compose up --build -d`

### YouTube Data API enrichment

For richer metadata (duration, tags, view count):

1. Create a Google Cloud project at https://console.cloud.google.com/
2. Enable the YouTube Data API v3
3. Create an API key and set `YOUTUBE_API_KEY` in `.env`
4. Set `YOUTUBE_API_ENABLED = True` in `config.py`

### Whisper Tier-2 transcription

For videos without captions:

1. In `requirements.txt`, uncomment `yt-dlp` and either `openai` (for API) or `openai-whisper` (for local)
2. In `Dockerfile`, uncomment the `ffmpeg` install line
3. Set `WHISPER_ENABLED = True` and `WHISPER_BACKEND = "api"` or `"local"` in `config.py`
4. If using the API: set `OPENAI_API_KEY` in `.env`
5. Rebuild: `docker compose up --build -d`

### Flask Dashboard

1. Uncomment the `dashboard` service in `docker-compose.yml`
2. Set `OUTPUT_BACKEND = "dashboard"` in `config.py`
3. Rebuild and restart
4. Visit http://localhost:5000

---

## Raspberry Pi Deployment Notes

This image uses `python:3.13-slim` which publishes `linux/arm64` builds —
no emulation or cross-compilation needed on a Pi 4 or Pi 5 running a 64-bit OS.

Recommended Pi setup:
- Raspberry Pi OS Lite (64-bit) or Ubuntu Server 24.04 LTS
- Docker installed via the official convenience script:
  `curl -fsSL https://get.docker.com | sh`
- The SQLite database persists in a Docker named volume — no external storage needed

Resource usage (typical):
- Idle: ~30 MB RAM, ~0% CPU
- During a poll cycle: ~80 MB RAM, brief CPU spike during summarization

---

## Project Structure

```
yt-monitor/
├── config.py           # All settings and feature flags
├── monitor.py          # Main orchestration loop
├── database.py         # SQLite persistence layer
├── channels.py         # RSS feed polling
├── transcripts.py      # 3-tier transcript extraction
├── summarizer.py       # Claude API integration
├── output/
│   ├── __init__.py     # Backend factory
│   ├── base.py         # Abstract backend interface
│   ├── trello_backend.py
│   └── dashboard_backend.py
├── dashboard/
│   ├── app.py          # Flask dashboard (stub)
│   └── templates/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── .env.example
└── README.md
```

---

## Cost Estimates

Using `claude-sonnet-4-5` with prompt caching enabled:

| Videos/month | Est. cost |
|---|---|
| 50 | ~$0.25 |
| 100 | ~$0.50 |
| 300 | ~$1.50 |

Prompt caching reduces the effective input token cost by ~90% for the
system prompt. The Batch API (when implemented) adds a further 50% discount.

---

## License

MIT
