"""Image content parts in budget admission: each `image_url` part reserves
`IMAGE_TOKEN_ESTIMATE` prompt tokens instead of the length of its URL (a
base64 screenshot would otherwise look like ~100k tokens and refuse cheap
runs)."""

from __future__ import annotations

from pathlib import Path

import pytest

from inferrail.budgets.enforcement import (
    _CHARS_PER_TOKEN_UPPER_BOUND,
    IMAGE_TOKEN_ESTIMATE,
    approx_char_count,
    approx_message_chars,
)
from test_field_policy import Upstream, _body, _client

SCREENSHOT = "data:image/png;base64," + "A" * 300_000


def _image(url: str = SCREENSHOT) -> dict[str, object]:
    return {"type": "image_url", "image_url": {"url": url}}


def test_image_part_counts_as_the_fixed_estimate_not_its_url() -> None:
    messages = [{"role": "user", "content": [{"type": "text", "text": "abc"}, _image()]}]

    assert approx_message_chars(messages) == (
        len("user") + len("text") + 3 + IMAGE_TOKEN_ESTIMATE * _CHARS_PER_TOKEN_UPPER_BOUND
    )


def test_each_image_adds_one_estimate() -> None:
    one = approx_message_chars([{"role": "user", "content": [_image()]}])
    three = approx_message_chars([{"role": "user", "content": [_image()] * 3}])

    assert three - one == 2 * IMAGE_TOKEN_ESTIMATE * _CHARS_PER_TOKEN_UPPER_BOUND


def test_text_only_messages_are_counted_as_before() -> None:
    messages = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
    ]

    assert approx_message_chars(messages) == approx_char_count(messages)


def test_screenshot_fits_a_small_run_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    upstream = Upstream()
    client = _client(monkeypatch, tmp_path, upstream)
    content = [{"type": "text", "text": "what is on this page?"}, _image()]

    response = client.post(
        "/v1/chat/completions",
        json=_body(messages=[{"role": "user", "content": content}], max_tokens=200),
        headers={"X-Inferrail-Attribute-Work-Id": "browse-1", "X-Inferrail-Budget-Usd": "0.01"},
    )

    assert response.status_code == 200, response.text
    assert len(upstream.bodies) == 1
