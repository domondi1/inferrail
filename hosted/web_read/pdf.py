"""PDF → text and metadata for Web Read (papers, filings, manuals, documentation).

Text comes from the PDF's own text layer (pypdf), page by page; nothing is OCRed, so a scanned PDF
with no text layer is refused for free (`no_readable_content`), like a JavaScript-only page.
Bounded so one document can't take the service down: at most MAX_PAGES pages are read, every
decompression filter is capped at DECOMPRESS_MAX bytes of output, and at most PARSE_SLOTS parses
run at once (in threads, each with a time limit).
"""

from __future__ import annotations

import asyncio
import io
import re
import threading
from datetime import datetime
from typing import Any

import pypdf

MAX_PAGES = 50
DECOMPRESS_MAX = 20_000_000  # bytes of output per stream (pypdf's default is 75 MB)
PARSE_TIMEOUT_S = 20.0
PARSE_SLOTS = 2

# pypdf 7 limits are set through its Configuration (the old module constants raise there).
_LIMITS = ("maximum_declared_stream_length", "array_based_stream_maximum_output_length",
           "jbig2_maximum_output_length", "lzw_maximum_output_length",
           "run_length_maximum_output_length", "zlib_maximum_output_length",
           "brotli_maximum_output_length", "image_maximum_buffer_size")  # fmt: skip
_config = pypdf.get_configuration()
pypdf.overwrite_configuration(type(_config)(**{
    **{k: getattr(_config, k) for k in _config.__dataclass_fields__},
    **{k: min(getattr(_config, k), DECOMPRESS_MAX) for k in _LIMITS if hasattr(_config, k)},
    "disable_legacy_handling": True,
}))  # fmt: skip

_slots = threading.BoundedSemaphore(PARSE_SLOTS)


class Unreadable(Exception):
    """The bytes aren't a PDF we can read (broken, encrypted, or over a limit)."""


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    return None


def _clean(value: Any, limit: int = 1000) -> str | None:
    if not isinstance(value, str):
        return None
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit] or None


def parse(body: bytes, max_pages: int = MAX_PAGES) -> tuple[str, str, dict[str, Any]]:
    """(title, text, metadata fields) of a PDF. Raises Unreadable."""
    try:
        reader = pypdf.PdfReader(io.BytesIO(body))
        if reader.is_encrypted and not reader.decrypt(""):
            raise Unreadable("encrypted")
        total = len(reader.pages)
        texts = []
        for page in reader.pages[:max_pages]:
            text = (page.extract_text() or "").strip()
            if text:
                texts.append(text)
        info = reader.metadata
    except Unreadable:
        raise
    except Exception as exc:  # noqa: BLE001 - any parser failure is a free refusal
        raise Unreadable(type(exc).__name__) from exc
    content = "\n\n".join(texts)
    if content and total > max_pages:
        content += f"\n\n[Only the first {max_pages} of {total} pages were read.]"
    first_line = next((ln.strip() for ln in content.splitlines() if ln.strip()), "")
    title = _clean(getattr(info, "title", None), 300) or _clean(first_line, 200) or ""
    author = _clean(getattr(info, "author", None), 300)
    fields: dict[str, Any] = {
        "description": _clean(getattr(info, "subject", None)),
        "authors": [author] if author else [],
        "published_at": _iso(getattr(info, "creation_date", None)),
        "modified_at": _iso(getattr(info, "modification_date", None)),
        "type": "PDF",
    }
    return title, content, fields


async def parse_async(body: bytes) -> tuple[str, str, dict[str, Any]]:
    """`parse` in a worker thread, at most PARSE_SLOTS at once, within PARSE_TIMEOUT_S."""

    def run() -> tuple[str, str, dict[str, Any]]:
        if not _slots.acquire(timeout=PARSE_TIMEOUT_S):
            raise Unreadable("busy")
        try:
            return parse(body)
        finally:
            _slots.release()

    try:
        return await asyncio.wait_for(asyncio.to_thread(run), PARSE_TIMEOUT_S * 2)
    except TimeoutError as exc:
        raise Unreadable("timeout") from exc
