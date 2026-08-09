"""Where telemetry events go.

``TelemetrySink`` is the only interface a future sink (SQLite, OpenTelemetry
exporter, Inferrail Cloud) needs to implement. v0.1 ships the two sinks
useful for a fully local, offline data plane: stdout (structured logging)
and a local JSONL file. Nothing here transmits data off the machine.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Protocol

from inferrail.config.models import TelemetryConfig
from inferrail.telemetry.events import InferenceEvent

_logger = logging.getLogger("inferrail.telemetry")


class TelemetrySink(Protocol):
    def emit(self, event: InferenceEvent) -> None: ...


class ConsoleTelemetrySink:
    """Logs each event as one JSON object via the standard logging module."""

    def emit(self, event: InferenceEvent) -> None:
        _logger.info(event.model_dump_json())


class JSONLTelemetrySink:
    """Appends each event as one JSON line to a local file.

    The file (and its parent directory) is created on first write if it
    doesn't already exist.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: InferenceEvent) -> None:
        with self._path.open("a", encoding="utf-8") as f:
            f.write(event.model_dump_json())
            f.write("\n")


class NullTelemetrySink:
    """Discards events. Used when telemetry.sink is 'none'."""

    def emit(self, event: InferenceEvent) -> None:  # noqa: D401
        pass


def build_telemetry_sink(config: TelemetryConfig) -> TelemetrySink:
    if config.sink == "console":
        return ConsoleTelemetrySink()
    if config.sink == "jsonl":
        assert config.path is not None  # enforced by TelemetryConfig validation
        return JSONLTelemetrySink(config.path)
    if config.sink == "none":
        return NullTelemetrySink()
    raise ValueError(f"unknown telemetry sink: {config.sink}")  # unreachable: config validated
