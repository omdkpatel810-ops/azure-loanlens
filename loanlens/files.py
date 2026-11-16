"""Checking an upload before it's stored: what it really is (from its first bytes, never its name or the
browser's say-so), how big it is, and whether a model will be able to read it."""

from __future__ import annotations

import re
import unicodedata

SIGNATURES = {
    "application/pdf": (b"%PDF-", ".pdf"),
    "image/jpeg": (b"\xff\xd8\xff", ".jpg"),
    "image/png": (b"\x89PNG\r\n\x1a\n", ".png"),
}


class Rejected(ValueError):
    """An upload LoanLens won't store. `reason` is a key into the portal's messages."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def sniff(data: bytes) -> tuple[str, str] | None:
    for content_type, (magic, ext) in SIGNATURES.items():
        if data.startswith(magic):
            return content_type, ext
    return None


def check(data: bytes, max_bytes: int) -> tuple[str, str]:
    """The upload's content type and file extension, or Rejected."""
    if not data:
        raise Rejected("empty")
    if len(data) > max_bytes:
        raise Rejected("too_big")
    found = sniff(data)
    if found is None:
        raise Rejected("wrong_type")
    if found[0] == "application/pdf" and b"/Encrypt" in data:
        raise Rejected("locked_pdf")
    return found


def clean_filename(name: str | None, fallback: str = "document") -> str:
    """A name safe to show back: no path, no control characters, at most 80 characters."""
    name = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = re.sub(r"[\x00-\x1f\x7f<>:\"|?*]", "", name).strip(" .")
    return name[:80] or fallback
