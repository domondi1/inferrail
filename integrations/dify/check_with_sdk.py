"""Check the Inferrail Dify plugin inside Dify's own plugin SDK, without a Dify instance.

    python -m venv /tmp/difyvenv
    /tmp/difyvenv/bin/pip install "dify_plugin==0.10.0" httpx pyyaml
    cd integrations/dify && /tmp/difyvenv/bin/python check_with_sdk.py inferrail_web_read

Validates manifest.yaml, the provider and tool YAML with the SDK's models, then runs the real
provider and tool classes against a local stand-in for Inferrail (no network, nothing paid).
Exits non-zero on any failure. Pass an unpacked .difypkg directory to check the package itself.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import httpx
import yaml

P = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(P))
from dify_plugin.core.entities.plugin.setup import PluginConfiguration  # noqa: E402
from dify_plugin.entities.tool import ToolConfiguration, ToolProviderConfiguration  # noqa: E402


def load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(name, P / rel)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


manifest = yaml.safe_load((P / "manifest.yaml").read_text())
PluginConfiguration(**manifest)
provider_yaml = manifest["plugins"]["tools"][0]
prov = yaml.safe_load((P / provider_yaml).read_text())
for tool_yaml in prov["tools"]:
    ToolConfiguration(**yaml.safe_load((P / tool_yaml).read_text()))
ToolProviderConfiguration(**{**prov, "tools": []})
print("ok: manifest, provider and tool YAML valid per dify_plugin models")

import inferrail_client as ic  # noqa: E402

state = {"balance": 4}


def handler(req: httpx.Request) -> httpx.Response:
    if req.headers.get("authorization") != "Bearer irw_ok":
        return httpx.Response(401, json={"error": "workspace_key_required"})
    if req.url.path == ic.WORKSPACE_PATH:
        return httpx.Response(200, json={"credits_remaining": state["balance"]})
    url = json.loads(req.content)["url"]
    if "down" in url:
        return httpx.Response(422, json={"error": "upstream_status", "charged": False})
    if state["balance"] < 2:
        return httpx.Response(402, json={
            "error": "insufficient_credits", "credits_needed": 2,
            "credits_remaining": state["balance"],
            "top_up": {"card": "POST https://x/hosted/v1/credits/checkout"}})  # fmt: skip
    state["balance"] -= 2
    return httpx.Response(200, json={"url": url, "title": "T", "content_markdown": "Body",
                                     "content_sha256": "h",
                                     "metadata": {"published_at": "2026-11-01"}})  # fmt: skip


real_client = httpx.Client
ic.httpx.Client = lambda **kw: real_client(transport=httpx.MockTransport(handler))  # type: ignore[assignment,misc]

provider_py = prov["extra"]["python"]["source"]
provider = next(v for v in vars(load("plugin_provider", provider_py)).values()
                if isinstance(v, type) and v.__name__.endswith("Provider")
                and v.__module__ == "plugin_provider")()  # fmt: skip
provider._validate_credentials({"api_key": "irw_ok"})
try:
    provider._validate_credentials({"api_key": "irw_bad"})
    raise SystemExit("FAIL: a bad key was accepted")
except SystemExit:
    raise
except Exception as ex:
    assert type(ex).__name__ == "ToolProviderCredentialValidationError", ex
print("ok: provider accepts a valid key and refuses a bad one")


def tool_class(rel: str):
    mod = load("plugin_" + Path(rel).stem, rel)
    return next(
        v
        for v in vars(mod).values()
        if isinstance(v, type) and v.__name__.endswith("Tool") and v.__module__ == mod.__name__
    )


def run(cls, params):
    tool = cls.from_credentials({"api_key": "irw_ok"})
    return [
        (m.type.value, getattr(m.message, "text", None) or getattr(m.message, "json_object", None))
        for m in tool._invoke(params)
    ]


ReadPage = tool_class("tools/read_page.py")
ReadPages = tool_class("tools/read_pages.py")
out = run(ReadPage, {"url": "https://a.test/"})
assert [t for t, _ in out] == ["text", "json"] and out[0][1].startswith("# T"), out
out = run(ReadPages, {"urls": "https://b.test/\nhttps://down.test/"})
assert out[1][1]["charged_pages"] == 1 and "not charged" in out[0][1], out
out = run(ReadPage, {"url": "https://c.test/"})
assert "Not enough Inferrail credits" in out[0][1], out
assert state["balance"] == 0
print("ok: read_page and read_pages deliver, charge only delivered pages, and explain no credits")
