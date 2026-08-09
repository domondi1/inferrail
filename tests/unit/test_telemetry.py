from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from inferrail.config.models import TelemetryConfig
from inferrail.telemetry.events import InferenceEvent
from inferrail.telemetry.sinks import (
    ConsoleTelemetrySink,
    JSONLTelemetrySink,
    NullTelemetrySink,
    build_telemetry_sink,
)


def _event(**overrides: object) -> InferenceEvent:
    defaults: dict[str, object] = dict(
        request_id="req_test",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        total_latency_ms=123.4,
        prompt_tokens=5,
        completion_tokens=3,
    )
    defaults.update(overrides)
    return InferenceEvent(**defaults)  # type: ignore[arg-type]


def test_inference_event_has_no_payload_fields() -> None:
    # Structural guarantee behind the "no prompts/responses by default"
    # privacy default: the event schema simply has nowhere to put them.
    field_names = set(InferenceEvent.model_fields)
    assert field_names.isdisjoint({"prompt", "messages", "content", "response"})


def test_console_sink_emits_structured_json(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="inferrail.telemetry")

    ConsoleTelemetrySink().emit(_event(request_id="req_console"))

    assert len(caplog.records) == 1
    payload = json.loads(caplog.records[0].getMessage())
    assert payload["request_id"] == "req_console"
    assert payload["provider"] == "openai"


def test_jsonl_sink_writes_one_line_per_event(tmp_path: Path) -> None:
    sink = JSONLTelemetrySink(tmp_path / "nested" / "telemetry.jsonl")

    sink.emit(_event(request_id="req_1"))
    sink.emit(_event(request_id="req_2"))

    lines = (tmp_path / "nested" / "telemetry.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["request_id"] == "req_1"
    assert json.loads(lines[1])["request_id"] == "req_2"


def test_null_sink_discards_silently() -> None:
    NullTelemetrySink().emit(_event())  # must not raise


def test_build_telemetry_sink_dispatches_by_config() -> None:
    assert isinstance(build_telemetry_sink(TelemetryConfig(sink="console")), ConsoleTelemetrySink)
    assert isinstance(build_telemetry_sink(TelemetryConfig(sink="none")), NullTelemetrySink)


def test_build_telemetry_sink_jsonl(tmp_path: Path) -> None:
    sink = build_telemetry_sink(TelemetryConfig(sink="jsonl", path=str(tmp_path / "t.jsonl")))
    assert isinstance(sink, JSONLTelemetrySink)
