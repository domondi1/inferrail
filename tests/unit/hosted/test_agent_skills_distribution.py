"""The skills in `skills/` are installed by other tools (the `skills` CLI into ~75 coding agents,
Claude Code as a plugin), which skip a skill whose front matter doesn't parse. Proves every skill
is well formed and points at the live contract, the plugin and marketplace manifests agree, and
the optional `X-Inferrail-Source` label is counted per channel without identity."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SKILLS = sorted((ROOT / "skills").glob("*/SKILL.md"))
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def _front(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n"), path
    return yaml.safe_load(text.split("---")[1])  # strict YAML, as the installers parse it


def test_there_are_skills_to_install():
    assert {p.parent.name for p in SKILLS} == {
        "inferrail-web-read", "inferrail-package-check", "inferrail-domain-check"}  # fmt: skip


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_each_skill_is_well_formed(path):
    front = _front(path)
    assert set(front) == {"name", "description"}
    assert front["name"] == path.parent.name and NAME.match(front["name"])  # spec: name = folder
    assert len(front["name"]) <= 64 and 50 <= len(front["description"]) <= 1024
    assert "$0.002" in front["description"] and "Use " in front["description"]


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_each_skill_stops_at_the_wallet_boundary_and_pins_its_client(path):
    text = path.read_text()
    assert "ask your user" in text.lower()  # before creating or funding a wallet
    assert "--max-amount" in text and "X-Inferrail-Source: skill" in text
    assert not re.search(r"npx agentcash(?!@\d)", text)  # every AgentCash call is pinned
    assert "https://inferrail-cost-gateway.onrender.com/" in text


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_each_skill_says_when_a_free_tool_is_better(path):
    # Don't send an agent to a paid call it doesn't need.
    text = path.read_text()
    assert "## When another tool is better" in text and "## When to call it" in text
    assert "awal" not in text  # an untested wallet command is not offered as a recipe


def test_the_plugin_and_marketplace_manifests_agree():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    (entry,) = market["plugins"]
    assert plugin["name"] == entry["name"] == "inferrail" and entry["source"] == "./"
    assert market["owner"]["name"] and re.match(r"^\d+\.\d+\.\d+$", plugin["version"])
    assert (ROOT / plugin["icon"]).is_file()
    assert plugin["license"] == "Apache-2.0"  # the repository's licence


def test_the_readme_shows_both_install_lines():
    readme = (ROOT / "README.md").read_text()
    assert "npx skills add domondi1/inferrail" in readme
    assert "/plugin install inferrail --marketplace domondi1/inferrail" in readme


# -- channel attribution -----------------------------------------------------------------------
pytest.importorskip("x402")
pytest.importorskip("eth_account")
sys.path.insert(0, str(Path(__file__).resolve().parent))
import httpx  # noqa: E402
from _x402_fake_facilitator import (  # noqa: E402
    FakeFacilitator,
    decode_payment_required,
    sign_payment,
)
from test_web_read import ARTICLE, HTML, ROBOTS_OK, _client  # noqa: E402


def _stats(c):
    return c.get("/v1/operator/stats", headers={"Authorization": "Bearer op-token"}).json()


def test_a_labelled_call_is_counted_per_channel(tmp_path):
    site = {"news.test/robots.txt": ROBOTS_OK,
            "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML)}  # fmt: skip
    c = _client(tmp_path, site, FakeFacilitator())
    tag = {"X-Inferrail-Source": "skill"}
    unpaid = c.post("/v1/page", json={"url": "https://news.test/a"}, headers=tag)
    signed = sign_payment(decode_payment_required(unpaid))
    paid = c.post("/v1/page", json={"url": "https://news.test/a"},
                  headers={**tag, "PAYMENT-SIGNATURE": signed.header})  # fmt: skip
    assert paid.status_code == 200
    channels = _stats(c)["funnel"]["by_channel"]
    assert channels == {"skill:challenge": 1, "skill:attempt": 1, "skill:delivered": 1}


@pytest.mark.parametrize("value", ["", "Skill-Evil", "a@b.c", "skill; drop", "x" * 200])
def test_an_unknown_label_is_ignored(tmp_path, value):
    c = _client(tmp_path, {}, FakeFacilitator())
    c.post("/v1/page", json={"url": "https://news.test/a"}, headers={"X-Inferrail-Source": value})
    assert _stats(c)["funnel"]["by_channel"] == {}


def test_ci_pipelines_are_their_own_channel(tmp_path):
    c = _client(tmp_path, {}, FakeFacilitator())
    c.post("/v1/page", json={"url": "https://news.test/a"}, headers={"X-Inferrail-Source": "ci"})
    assert _stats(c)["funnel"]["by_channel"] == {"ci:challenge": 1}


def test_unlabelled_calls_are_counted_as_before(tmp_path):
    c = _client(tmp_path, {}, FakeFacilitator())
    c.post("/v1/page", json={"url": "https://news.test/a"})
    funnel = _stats(c)["funnel"]
    assert funnel["challenge"] == 1 and funnel["by_channel"] == {}
