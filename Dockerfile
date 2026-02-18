# ── yt-monitor Dockerfile ──────────────────────────────────────────────────
#
# Uses python:3.13-slim which publishes multi-architecture images:
#   linux/amd64  — laptops, workstations, standard VPS
#   linux/arm64  — Raspberry Pi 4/5 (64-bit OS required)
#
# Build: docker build -t yt-monitor .
# Run:   docker compose up -d
# ────────────────────────────────────────────────────────────────────────────

FROM python:3.13-slim

# Metadata
LABEL maintainer="yt-monitor"
LABEL description="YouTube channel monitor with AI summarization"

# Set timezone (adjust as needed)
ENV TZ=America/Chicago
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies
# ffmpeg is commented out — only needed when WHISPER_ENABLED=True
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    # ffmpeg \    ← uncomment when enabling Whisper Tier-2 transcription
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (layer caching: only rebuilds when
# requirements.txt changes, not on every code change)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories for persistent data and logs
# These are mounted as Docker volumes so data survives container restarts
RUN mkdir -p /app/data /app/logs

# Run as non-root user for security
RUN useradd --no-create-home --shell /bin/false appuser \
    && chown -R appuser:appuser /app
USER appuser

# Health check — verifies the Python environment is intact
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import feedparser, anthropic, youtube_transcript_api; print('ok')" || exit 1

# Default entrypoint: run the monitor
CMD ["python", "monitor.py"]
