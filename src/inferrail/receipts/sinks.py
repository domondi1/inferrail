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
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(receipt.model_dump_json())
                f.write("\n")
        except OSError as exc:
            _logger.warning(
                "failed to write receipt %s to %s: %s", receipt.receipt_id, self._path, exc
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
