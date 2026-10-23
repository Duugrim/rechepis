"""Turning segment texts into one transcript."""

from __future__ import annotations


def join_segment_texts(items: list[tuple[str, float]], paragraph_gap: float) -> str:
    """``items`` are ``(text, silence_before_seconds)`` in order. Short silence: same paragraph."""
    out = ""
    for text, gap_before in items:
        text = text.strip()
        if not text:
            continue
        if out:
            out += "\n\n" if gap_before >= paragraph_gap else " "
        out += text
    return out
