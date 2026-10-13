"""Telegram shows the owner's messages as plain text, so Markdown would show up as stray symbols ("**Director**").
Models fall back on Markdown, especially after reading Markdown sources, so every outgoing message is cleaned here,
mechanically, instead of relying on a prompt rule alone."""

from __future__ import annotations

import re


def to_plain(text: str) -> str:
    text = re.sub(r"(?m)^\s*```.*$\n?", "", text)  # code fences
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1: \2", text)  # [text](url) -> text: url
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text, flags=re.S)  # **bold**
    text = re.sub(r"(?<![\w/])__(.+?)__(?![\w/])", r"\1", text, flags=re.S)  # __bold__ (not inside URLs)
    text = re.sub(r"(?<![\w*/])\*(?=\S)([^*\n]+?)(?<=\S)\*(?![\w*])", r"\1", text)  # *italic*
    text = re.sub(r"`([^`\n]+)`", r"\1", text)  # `code`
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+", "", text)  # headings
    text = re.sub(r"(?m)^(\s*)[*•]\s+", r"\1- ", text)  # bullets -> "-"
    text = re.sub(r"(?m)^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$\n?", "", text)  # table separators
    text = re.sub(r"(?m)^\s*\|(.+)\|\s*$", lambda m: " · ".join(c.strip() for c in m.group(1).split("|")), text)  # table rows
    return text
