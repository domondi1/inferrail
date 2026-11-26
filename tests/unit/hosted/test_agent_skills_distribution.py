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


APPROVED = {  # each service's approved receiving addresses (preflight checks the same ones)
    "inferrail-web-read": "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E",
    "inferrail-package-check": "0xe9c1249648257C1C1D70F8b7682851C377bb8f53",
    "inferrail-domain-check": "0xb95D56f169324A0B23B82bd3975635Cf0187ce07",
}
TEMPO = "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E"


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_each_skill_asks_before_any_wallet_is_created(path):
    text = path.read_text()
    ask = text.index("Ask your user before the first AgentCash command")
    assert ask < text.index("npx agentcash@0.17.1 balance")  # consent comes first
    assert "test -f ~/.agentcash/wallet.json" in text


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_each_skill_names_who_it_pays(path):
    who = path.read_text().split("**Who you pay.**")[1].split("\n\n")[0]
    assert APPROVED[path.parent.name] in who and TEMPO in who
    assert "eip155:8453" in who and "4217" in who


@pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parent.name)
def test_an_untested_wallet_is_labelled_the_same_way_and_not_claimed(path):
    text = path.read_text()
    assert "Coinbase agentic wallet" not in _front(path)["description"]
    assert "we haven't tested that one with this endpoint" in text


def test_the_plugin_and_marketplace_manifests_agree():
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    (entry,) = market["plugins"]
    assert plugin["name"] == entry["name"] == "inferrail" and entry["source"] == "./"
    assert market["owner"]["name"] and re.match(r"^\d+\.\d+\.\d+$", plugin["version"])
    assert (ROOT / plugin["icon"]).is_file()
    assert plugin["license"] == "Apache-2.0"  # the repository's licence


def test_a_skill_change_comes_with_a_new_plugin_version():
    # Claude Code updates an installed plugin only when its version changes, so changed skills
    # under an old version would never reach anyone who installed before.
    import hashlib

    digest = hashlib.sha256()
    for path in SKILLS:
        rel = path.relative_to(ROOT).as_posix()
        digest.update(rel.encode() + b"\0" + path.read_bytes() + b"\0")
    release = json.loads((ROOT / ".claude-plugin" / "release.json").read_text())
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    assert release["skills_sha256"] == digest.hexdigest(), (
        "skills changed: bump .claude-plugin/plugin.json version and record it, with the new "
        "hash, in .claude-plugin/release.json")
    assert release["version"] == plugin["version"]


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


def test_the_ai_sdk_tools_package_is_its_own_channel(tmp_path):
    c = _client(tmp_path, {}, FakeFacilitator())
    c.post("/v1/page", json={"url": "https://news.test/a"},
           headers={"X-Inferrail-Source": "ai-sdk"})  # fmt: skip
    assert _stats(c)["funnel"]["by_channel"] == {"ai-sdk:challenge": 1}
    tools = (ROOT / "packages" / "ai-sdk-tools" / "src" / "core.ts").read_text()
    assert '"x-inferrail-source": "ai-sdk"' in tools


def test_the_local_mcp_server_is_its_own_channel(tmp_path):
    c = _client(tmp_path, {}, FakeFacilitator())
    c.post("/v1/page", json={"url": "https://news.test/a"},
           headers={"X-Inferrail-Source": "mcp-local"})  # fmt: skip
    assert _stats(c)["funnel"]["by_channel"] == {"mcp-local:challenge": 1}
    server = (ROOT / "packages" / "ai-sdk-tools" / "src" / "mcp.ts").read_text()
    assert 'export const SOURCE = "mcp-local";' in server


def test_unlabelled_calls_are_counted_as_before(tmp_path):
    c = _client(tmp_path, {}, FakeFacilitator())
    c.post("/v1/page", json={"url": "https://news.test/a"})
    funnel = _stats(c)["funnel"]
    assert funnel["challenge"] == 1 and funnel["by_channel"] == {}
