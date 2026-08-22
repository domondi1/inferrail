"""Where `InferenceReceipt` records go.

Deliberately parallel to `telemetry.sinks`, and deliberately not sharing
code with it: the two schemas serve different audiences (operational
debugging vs. economic reporting) and are allowed to evolve independently.
v0.1 ships only a JSONL sink — the receipt is meant to be read back by
`inferrail report`, so a console-only sink would be a dead end. Nothing
here transmits data off the machine.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol

from inferrail.config.models import ReceiptsConfig
from inferrail.receipts.schema import InferenceReceipt

_logger = logging.getLogger("inferrail.receipts")


class ReceiptSink(Protocol):
    def emit(self, receipt: InferenceReceipt) -> None: ...


class JSONLReceiptSink:
    """Appends each receipt as one JSON line to a local file.

    The file (and its parent directory) is created on first write if it
    doesn't already exist.

    One receipt is one `os.write` to an `O_APPEND` descriptor, never a
    buffered `write()` pair. Buffered text I/O splits a record larger than
    the ~8 KiB buffer into several syscalls, so two concurrent writers
    interleave *within* a line and both records are lost to a
    `JSONDecodeError` on read back — a receipt large enough to hit this is
    reachable with ordinary attribution (many attributes, or long values),
    and losing accounting records is exactly what this ledger may not do.
    `O_APPEND` makes each single write atomic against other appenders, so
    this holds across processes (multiple workers on one receipts file)
    as well as threads.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, receipt: InferenceReceipt) -> None:
        # receipts.sink defaults to "jsonl" (see ReceiptsConfig), so this
        # runs on every request unless an operator opts out. A write
        # failure here (read-only filesystem, permissions, disk full, a
        # missing directory created after startup) must never crash an
        # otherwise-successful — or independently-failed — request: this is
        # an accounting side-channel, not the response path. Losing one
        # receipt is logged loudly, not silently swallowed, but it cannot
        # be allowed to turn a completed inference into a 500.
        line = (receipt.model_dump_json() + "\n").encode("utf-8")
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                written = os.write(fd, line)
            finally:
                os.close(fd)
        except OSError as exc:
            _logger.warning(
                "failed to write receipt %s to %s: %s", receipt.receipt_id, self._path, exc
            )
            return
        if written != len(line):
            # A short write leaves a truncated line that would fail to
            # parse later; say so now rather than let it surface as an
            # unexplained skipped row in `inferrail report`.
            _logger.warning(
                "receipt %s was partially written to %s (%d of %d bytes)",
                receipt.receipt_id, self._path, written, len(line),
            )


class NullReceiptSink:
    """Discards receipts. Used when receipts.sink is 'none'."""

    def emit(self, receipt: InferenceReceipt) -> None:  # noqa: D401
        pass


def build_receipt_sink(config: ReceiptsConfig) -> ReceiptSink:
    if config.sink == "jsonl":
        return JSONLReceiptSink(config.path)
    if config.sink == "none":
        return NullReceiptSink()
    raise ValueError(f"unknown receipts sink: {config.sink}")  # unreachable: config validated
