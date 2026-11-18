"""Titles, descriptions and tags. Uses Claude for punchy hooks if ANTHROPIC_API_KEY is set,
otherwise falls back to a template built from the transcript / source title."""
from __future__ import annotations

import os
import re

from .net import request

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-haiku-5-5")


class _SafeDict(dict):
    def __missing__(self, key):
        return ""


def fill(template: str, **values) -> str:
    return template.format_map(_SafeDict(**values)).strip()


def transcript_for(words, start: float, end: float) -> str:
    if not words:
        return ""
    return " ".join(w for t, w in words if start <= t < end and not w.startswith("["))


def make_hook(*, source_title: str, creator: str, transcript: str, index: int) -> str:
    hook = None
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            hook = _claude_hook(source_title, creator, transcript)
        except Exception as e:  # never let titling break the pipeline
            print(f"  ! Claude title failed, using template: {e}")
    if not hook:
        hook = _template_hook(source_title, transcript, index)
    return _clean(hook)[:70]


def _claude_hook(source_title: str, creator: str, transcript: str) -> str | None:
    prompt = (
        "You write titles for YouTube Shorts clipped from GTA 6 videos.\n"
        f"Creator: {creator}\nOriginal video title: {source_title}\n"
        f"What is said in the clip: {transcript[:1500] or '(no transcript)'}\n\n"
        "Write ONE curiosity-driven title, max 60 characters, describing what actually happens "
        "in this clip. No hashtags, no quotes, no clickbait that the clip does not deliver. "
        "Reply with the title only."
    )
    out = request("POST", "https://api.anthropic.com/v1/messages", timeout=60, data={
        "model": CLAUDE_MODEL, "max_tokens": 60,
        "messages": [{"role": "user", "content": prompt}],
    }, headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"],
                "anthropic-version": "2023-06-01"})
    text = "".join(b.get("text", "") for b in out.get("content", []) if b.get("type") == "text")
    return text.strip().strip('"').splitlines()[0] if text.strip() else None


def _template_hook(source_title: str, transcript: str, index: int) -> str:
    # Prefer an exclamation from the transcript ("NO WAY HE DID THAT")
    if transcript:
        sentences = re.split(r"(?<=[.!?])\s+", transcript)
        punchy = [s for s in sentences if 12 <= len(s) <= 60 and ("!" in s or "?" in s)]
        if punchy:
            return punchy[0]
    base = re.sub(r"\s*[\|\-–—]\s*.*$", "", source_title or "").strip() or "GTA 6 moment"
    return f"{base} (Part {index})" if index > 1 else base


def _clean(s: str) -> str:
    s = re.sub(r"#\w+", "", s)
    s = s.replace("<", "").replace(">", "")
    return re.sub(r"\s+", " ", s).strip()


def hashtags(tags: list[str], limit: int = 5) -> str:
    out = []
    for t in tags:
        t = re.sub(r"[^A-Za-z0-9]", "", t)
        if t and f"#{t}" not in out:
            out.append(f"#{t}")
    return " ".join(out[:limit])
