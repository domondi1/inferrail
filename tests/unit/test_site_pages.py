"""Static checks on the public site (docs/*.html). The pages have no build
step or JS test runner, so these read the HTML directly."""

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parents[2] / "docs"
HOME = (DOCS / "index.html").read_text(encoding="utf-8")
TRY = (DOCS / "try" / "index.html").read_text(encoding="utf-8")
AUTH_PY = (DOCS.parent / "hosted" / "cost_gateway" / "auth.py").read_text(encoding="utf-8")

# Every element the /try/ script drives. Moving one behind a disclosure is
# fine; losing one silently breaks a working feature.
TRY_PAGE_IDS = [
    "work-id-input", "task-id-input", "run-demo-btn", "send-demo-btn", "send-real-btn",
    "openai-key-input", "anthropic-key-input", "key-form", "submit-keys-btn", "forget-keys-btn",
    "key-status", "base-url-code", "dashboard-link-code", "snippet-curl", "snippet-openai-py",
    "snippet-anthropic-py", "snippet-langchain", "snippet-fetch", "feed-list", "budget-spent",
    "budget-cap", "budget-fill", "work-summary", "work-table", "work-rows", "task-table",
    "task-rows", "download-data-btn", "download-status", "feedback-form", "feedback-message",
    "feedback-contact", "feedback-status", "end-trial-btn", "countdown-value", "mode-badge",
    "trial-notice", "restore-note", "unreachable-banner", "trial-panel", "result",
]


def _js_const(name: str) -> int:
    match = re.search(rf"const {name} = (\d+);", TRY)
    assert match, f"{name} not found in docs/try/index.html"
    return int(match.group(1))


def test_home_leads_with_the_job_cost_story():
    hero = re.search(r'<h1[^>]*id="hero-title"[^>]*>(.*?)</h1>', HOME, re.S)
    assert hero
    text = re.sub(r"<[^>]+>", " ", hero.group(1))
    assert " ".join(text.split()) == "20 model calls. One job. One cost."
    assert "Know what your AI work costs." in HOME
    assert "Without keeping what it said." in HOME


def test_home_example_card_is_labelled_as_an_example():
    assert "Example</span>" in HOME
    assert "An example job, for illustration." in HOME


def test_try_page_keeps_every_scripted_element():
    for element_id in TRY_PAGE_IDS:
        assert f'id="{element_id}"' in TRY, element_id


def test_demo_job_plus_idle_polling_stays_within_the_rate_limit():
    """One demo run (N calls + 2 reads + 1 status) on top of a minute of idle
    polling must fit inside the per-trial limit with room to spare."""
    steps = len(re.findall(r"\{ task: '[a-z-]+', label: ", TRY))
    assert steps >= 2
    ticks = 60_000 / _js_const("POLL_INTERVAL_MS")
    idle = ticks + (ticks / _js_const("FULL_REFRESH_EVERY_N_POLLS")) * 3
    limit = re.search(r'"COST_GATEWAY_RATE_LIMIT_MAX_REQUESTS", "(\d+)"', AUTH_PY)
    assert limit
    max_requests = int(limit.group(1))
    assert idle + steps + 3 <= max_requests * 0.6


def test_labs_items_are_marked_testnet():
    labs = (DOCS / "labs" / "index.html").read_text(encoding="utf-8")
    assert labs.count("Base Sepolia testnet only") >= 2
    assert "not real-world spend enforcement" in labs


def test_no_em_dashes_in_redesigned_pages():
    for page in ("index.html", "try/index.html", "labs/index.html", "ap/index.html"):
        assert "—" not in (DOCS / page).read_text(encoding="utf-8"), page


def test_try_page_inlines_an_exact_copy_of_site_css():
    """/try/ inlines docs/site.css (no render-blocking stylesheet); the copy
    must stay identical to the shared file."""
    inline = re.search(r'<style id="site-css">\n(.*?)  </style>', TRY, re.S)
    assert inline
    assert inline.group(1) == (DOCS / "site.css").read_text(encoding="utf-8")
