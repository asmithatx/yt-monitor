"""
summarizer.py — Claude API summarization for yt-monitor.

Features:
  - Prompt caching via cache_control (saves ~90% on repeated system-prompt tokens)
  - Tier-aware prompting (adjusts tone for captions vs metadata-only)
  - Batch API support stub (enabled via BATCH_API_ENABLED in config.py)
  - Structured output with sections Claude returns consistently
"""

import logging
from dataclasses import dataclass
from typing import Optional

import anthropic

import config
from transcripts import TranscriptResult

logger = logging.getLogger(__name__)

# ── Lazy singleton client ────────────────────────────────────────────────────
_client: Optional[anthropic.Anthropic] = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise ValueError("ANTHROPIC_API_KEY is not set in .env")
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


# ────────────────────────────────────────────────────────────
#  Prompt templates
# ────────────────────────────────────────────────────────────

# The system prompt is cached server-side by Anthropic after the first call.
# This saves ~90% on input tokens for every subsequent call within 5 minutes.
_SYSTEM_PROMPT = """\
You are an expert content analyst specialising in YouTube video summarization. \
Your audience is a content creator who wants to quickly understand what their \
peers and competitors are publishing so they can identify trends, gaps, and opportunities.

When summarising, follow these rules:
1. Be concise and factual. Do not add opinions or value judgements.
2. Organise your output exactly as specified in the user message.
3. For auto-generated transcripts: the input may lack punctuation or contain \
filler words — focus on substance, not surface-level errors.
4. For metadata-only summaries: clearly note the summary is limited and \
derived from the title and description, not the full video.
5. Keep the total response under 600 words."""

# Instruct Claude to produce structured Markdown we can parse later
_USER_PROMPT_TEMPLATE = """\
Please summarise the following YouTube video.

**Channel:** {channel_name}
**Title:** {title}
**Source:** {source_label}

<transcript>
{transcript_text}
</transcript>

Produce your summary in exactly this format (use these Markdown headings):

## Overview
(2–3 sentences covering what the video is about)

## Key Points
(3–7 bullet points — the most important ideas, findings, or arguments)

## Notable Quotes or Claims
(1–3 direct quotes or strong claims from the transcript, or "None identified" \
if the source is metadata-only)

## Takeaways for Content Creators
(2–4 bullet points: trends observed, topics gaining traction, \
or gaps a creator could address)

## Tags
(5–10 single-word or short-phrase topic tags, comma-separated)
"""

# Source labels passed to the prompt so Claude knows what it's working with
_SOURCE_LABELS = {
    1: "Full transcript (manual captions)",
    2: "Full transcript (auto-generated via Whisper)",
    3: "⚠️ Metadata only (title + description) — transcript unavailable",
}


# ────────────────────────────────────────────────────────────
#  Result dataclass
# ────────────────────────────────────────────────────────────

@dataclass
class SummaryResult:
    text: str
    tokens_input: int
    tokens_output: int

    @property
    def estimated_cost_usd(self) -> float:
        """
        Approximate cost in USD using claude-sonnet-4-5 pricing.
        Input: $3.00 / 1M tokens
        Output: $15.00 / 1M tokens
        NOTE: Prompt caching reduces effective input cost significantly.
        """
        return (self.tokens_input / 1_000_000 * 3.00) + \
               (self.tokens_output / 1_000_000 * 15.00)


# ────────────────────────────────────────────────────────────
#  Real-time summarization
# ────────────────────────────────────────────────────────────

def summarize(
    video_id: str,
    channel_name: str,
    title: str,
    transcript: TranscriptResult,
) -> SummaryResult:
    """
    Call the Claude API synchronously and return a SummaryResult.
    Uses prompt caching on the system prompt.

    Raises:
        anthropic.APIError: On API-level failures (already has built-in
            exponential backoff retries for rate limits in the SDK).
    """
    if config.BATCH_API_ENABLED:
        logger.warning(
            "BATCH_API_ENABLED=True but batch mode is not yet implemented for "
            "real-time calling. Falling back to synchronous summarization."
        )

    client = _get_client()

    source_label = _SOURCE_LABELS.get(transcript.tier, f"Tier {transcript.tier}")
    user_prompt = _USER_PROMPT_TEMPLATE.format(
        channel_name=channel_name,
        title=title,
        source_label=source_label,
        transcript_text=transcript.text,
    )

    logger.debug(
        "Calling Claude for %s (tier=%d, chars=%d)",
        video_id, transcript.tier, len(transcript.text)
    )

    response = client.messages.create(
        model=config.CLAUDE_MODEL,
        max_tokens=config.SUMMARY_MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": _SYSTEM_PROMPT,
                # Prompt caching: Anthropic caches this block server-side.
                # Cached reads cost 0.1x the base input price.
                # The cache TTL is 5 minutes; resets on each API call.
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {"role": "user", "content": user_prompt}
        ],
    )

    summary_text = response.content[0].text
    usage = response.usage

    result = SummaryResult(
        text=summary_text,
        tokens_input=usage.input_tokens,
        tokens_output=usage.output_tokens,
    )

    logger.info(
        "Summary done for %s — %d in / %d out tokens (~$%.4f)",
        video_id,
        result.tokens_input,
        result.tokens_output,
        result.estimated_cost_usd,
    )

    return result


# ────────────────────────────────────────────────────────────
#  Batch API stub (BATCH_API_ENABLED=True activates this path)
# ────────────────────────────────────────────────────────────

def submit_batch_request(
    video_id: str,
    channel_name: str,
    title: str,
    transcript: TranscriptResult,
) -> str:
    """
    Submit a single request to Anthropic's Message Batches API.
    Returns a batch_request_id that can be polled later.

    ⚠️  NOT YET IMPLEMENTED — placeholder for future development.
    See: https://docs.anthropic.com/en/docs/build-with-claude/message-batches

    When implemented, this function will:
    1. Accumulate requests across multiple videos in one run.
    2. Submit them as a single batch (50% cheaper, <24hr turnaround).
    3. A separate poll_batch_results() job will retrieve and store them.
    """
    raise NotImplementedError(
        "Batch API support is not yet implemented. "
        "Set BATCH_API_ENABLED=False in config.py to use real-time summarization."
    )


def poll_batch_results() -> None:
    """
    Check pending Anthropic batch jobs and store results.

    ⚠️  NOT YET IMPLEMENTED — placeholder for future development.
    """
    raise NotImplementedError("Batch API polling is not yet implemented.")
