"""The Dify plugin's HTTP client (integrations/dify/inferrail_web_read/inferrail_client.py),
against a mocked Inferrail: the credits route, key validation, refusals and the markdown it
hands to Dify. No network, nothing paid."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[3]
PLUGIN = ROOT / "integrations" / "dify" / "inferrail_web_read"
spec = importlib.util.spec_from_file_location("inferrail_client", PLUGIN / "inferrail_client.py")
assert spec is not None and spec.loader is not None
client_mod = importlib.util.module_from_spec(spec)
sys.modules["inferrail_client"] = client_mod
spec.loader.exec_module(client_mod)

KEY = "irw_test"


def _client(balance: int = 10, broken: tuple[str, ...] = ()):
    seen: list[httpx.Request] = []
    state = {"balance": balance}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return httpx.Response(401, json={"error": "workspace_key_required"})
        if request.url.path == client_mod.WORKSPACE_PATH:
            return httpx.Response(200, json={"credits_remaining": state["balance"]})
        body = json.loads(request.content)
        if body["url"] in broken:
            return httpx.Response(422, json={"error": "upstream_status", "charged": False})
        if state["balance"] < 2:
            return httpx.Response(402, json={"error": "insufficient_credits", "charged": False,
                                             "credits_needed": 2, "credits_remaining": state["balance"],
                                             "top_up": {"card": "POST https://x/hosted/v1/credits/checkout"}})  # fmt: skip
        state["balance"] -= 2
        return httpx.Response(200, json={"url": body["url"], "final_url": body["url"], "title": "Title",
                                         "content_markdown": "Body text", "content_sha256": "abc",
                                         "metadata": {"published_at": "2026-11-01", "authors": ["A", "B"]}})  # fmt: skip

    return httpx.Client(transport=httpx.MockTransport(handler)), seen, state


def test_a_page_goes_to_the_credits_route_with_the_key_and_the_dify_label():
    c, seen, state = _client()
    page = client_mod.read_page(KEY, "https://a.test/", request_id="r1", client=c)
    assert page["ok"] is True and state["balance"] == 8
    req = seen[0]
    assert req.url.path == "/read/credits/v1/page" and req.headers["x-inferrail-source"] == "dify"
    assert json.loads(req.content) == {"url": "https://a.test/", "request_id": "r1"}


def test_an_unreadable_page_is_reported_and_costs_nothing():
    c, _, state = _client(broken=("https://down.test/",))
    page = client_mod.read_page(KEY, "https://down.test/", client=c)
    assert page == {"ok": False, "charged": False, "url": "https://down.test/", "status": 422,
                    "error": "upstream_status", "detail": ""}  # fmt: skip
    assert state["balance"] == 10


def test_too_few_credits_and_bad_keys_tell_the_user_what_to_do():
    c, _, _ = _client(balance=1)
    with pytest.raises(client_mod.InferrailError, match="checkout"):
        client_mod.read_page(KEY, "https://a.test/", client=c)
    c, _, _ = _client()
    with pytest.raises(client_mod.InferrailError, match="not valid"):
        client_mod.read_page("irw_wrong", "https://a.test/", client=c)


def test_key_validation_is_free_and_rejects_non_inferrail_keys():
    c, seen, state = _client(balance=10)
    assert client_mod.check_key(KEY, client=c) == 10
    assert seen[0].method == "GET" and state["balance"] == 10
    with pytest.raises(client_mod.InferrailError, match="irw_"):
        client_mod.check_key("sk-not-ours")


def test_urls_are_parsed_deduplicated_and_capped():
    assert client_mod.parse_urls("https://a.test/, https://b.test/\nhttps://a.test/") == [
        "https://a.test/", "https://b.test/"]
    with pytest.raises(client_mod.InferrailError):
        client_mod.parse_urls("\n".join(f"https://x.test/{i}" for i in range(11)))


def test_markdown_carries_provenance():
    md = client_mod.to_markdown({"url": "https://a.test/", "final_url": "https://a.test/p", "title": "T",
                                 "content_markdown": "Body", "metadata": {"published_at": "2026-11-01",
                                                                          "authors": ["A", "B"]}})  # fmt: skip
    assert md.splitlines()[:4] == ["# T", "Source: https://a.test/p", "Published: 2026-11-01", "Authors: A, B"]
    assert md.endswith("Body")


def test_the_manifest_declares_network_tags_privacy_and_only_our_domain():
    import yaml

    manifest = yaml.safe_load((PLUGIN / "manifest.yaml").read_text())
    assert manifest["author"] == "inferrail" and manifest["name"] == "inferrail_web_read"
    assert set(manifest["tags"]) <= {"search", "rag", "document", "utilities"}
    assert manifest["network"]["domains"] == ["inferrail-cost-gateway.onrender.com"]
    assert (PLUGIN / manifest["privacy"]).exists() and (PLUGIN / "_assets" / manifest["icon"]).exists()
    provider = yaml.safe_load((PLUGIN / "provider" / "inferrail.yaml").read_text())
    assert provider["credentials_for_provider"]["api_key"]["type"] == "secret-input"
    for tool in provider["tools"]:
        assert (PLUGIN / tool).exists() and (PLUGIN / tool.replace(".yaml", ".py")).exists()
