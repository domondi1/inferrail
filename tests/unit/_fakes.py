"""Shared `Provider` test double used by test_gateway.py and
test_streaming.py. Not a test module itself (no `test_` prefix, so pytest
never collects it) — plain file-local import, matching how this flat
`tests/unit/` directory (no `__init__.py` packages) makes every file here
importable by its bare module name from any other file in the same
directory.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse


@dataclass
class StreamScript:
    """One scripted `FakeProvider.stream()` run: some chunks, then either a
    clean end or an error raised after those chunks were already yielded —
    letting tests exercise the pre-first-byte-vs-after-first-byte retry
    boundary precisely.
    """

    chunks: list[bytes] = field(default_factory=list)
    error: Exception | None = None


class FakeProvider:
    """A `Provider` test double: no network, fully scriptable outcomes."""

    name = "openai"

    def __init__(
        self,
        outcomes: list[Any] | None = None,
        stream_outcomes: list[StreamScript] | None = None,
    ) -> None:
        # Each entry is either a NormalizedChatResponse to return, or an
        # exception instance to raise. Defaults to one canned success.
        self._outcomes = outcomes or [
            NormalizedChatResponse(
                content="ok", finish_reason="stop", prompt_tokens=3, completion_tokens=2
            )
        ]
        self._stream_outcomes = stream_outcomes or []
        self.calls: list[NormalizedChatRequest] = []
        self.stream_calls: list[NormalizedChatRequest] = []
        #: Incremented in a `finally`, so it counts both a clean end *and*
        #: an early `.aclose()` (client disconnect) — see test_streaming.py.
        self.stream_closed_count = 0

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        self.calls.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def stream(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> AsyncGenerator[bytes, None]:
        self.stream_calls.append(request)
        script = self._stream_outcomes.pop(0)
        try:
            for chunk in script.chunks:
                yield chunk
            if script.error is not None:
                raise script.error
        finally:
            self.stream_closed_count += 1
