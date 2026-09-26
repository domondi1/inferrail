"""Static checks on the hosted trial page (docs/try/index.html) that guard
its startup latency and its request budget against the Cost Gateway's
per-trial rate limit. The page has no build step or JS test runner, so
these read the HTML directly; the live, browser-driven measurement is
scripts/bench_try_page.py."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PAGE = (REPO_ROOT / "docs" / "try" / "index.html").read_text(encoding="utf-8")
AUTH_PY = (REPO_ROOT / "hosted" / "cost_gateway" / "auth.py").read_text(encoding="utf-8")


def _js_const(name: str) -> int:
    match = re.search(rf"const {name} = (\d+);", PAGE)
    assert match, f"{name} not found in docs/try/index.html"
    return int(match.group(1))


def _default_rate_limit_per_minute() -> float:
    max_requests = re.search(r'"COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS", "(\d+)"', AUTH_PY)
    window = re.search(r'"COST_GATEWAY_RATE_LIMIT_WINDOW_SECONDS", "(\d+)"', AUTH_PY)
    assert max_requests and window
    return int(max_requests.group(1)) * 60 / int(window.group(1))


def test_idle_polling_uses_at_most_a_third_of_the_rate_limit():
    """An idle visible tab must leave most of the per-trial rate limit for
    the visitor's own requests. (Before this budget existed, idle polling
    used 48 of 60 requests/minute, and a few demo clicks produced 429s.)
    Per idle tick: 1 call (receipts); every Nth tick also status + work +
    task report (3 more)."""
    ticks_per_minute = 60_000 / _js_const("POLL_INTERVAL_MS")
    full_every = _js_const("FULL_REFRESH_EVERY_N_POLLS")
    idle_requests_per_minute = ticks_per_minute * 1 + (ticks_per_minute / full_every) * 3
    assert idle_requests_per_minute <= _default_rate_limit_per_minute() / 3


def test_fresh_trial_does_not_fetch_its_empty_data():
    """A trial minted a moment ago has no receipts or work; fetching them
    right after POST /v1/trial only delays the visitor's first click."""
    assert "showTrial({ fresh: true })" in PAGE
    assert "if (!fresh) { refreshReceipts(); refreshWork(); }" in PAGE


def test_gateway_origin_is_preconnected():
    base_url = re.search(r"const DEFAULT_BASE_URL = '([^']+)';", PAGE)
    assert base_url
    assert f'<link rel="preconnect" href="{base_url.group(1)}" crossorigin>' in PAGE


def test_no_render_blocking_stylesheet():
    """Every <link rel="stylesheet"> outside <noscript> blocks first paint."""
    head = PAGE.split("</head>", 1)[0]
    head_without_noscript = re.sub(r"<noscript>.*?</noscript>", "", head, flags=re.S)
    assert 'rel="stylesheet"' not in head_without_noscript


def test_no_free_tier_cold_start_copy():
    """The service is always on; a slow or failed start gets an error and
    retry, never a "waking up" explanation."""
    lowered = PAGE.lower()
    for phrase in ("cold start", "waking up", "wake up"):
        assert phrase not in lowered


def test_trial_start_has_a_timeout_with_retry():
    assert _js_const("START_TRIAL_TIMEOUT_MS") <= 30_000
    assert "controller.abort()" in PAGE
    assert "btn.textContent = 'Try again'" in PAGE
