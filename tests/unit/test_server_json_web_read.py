"""The MCP Registry entry for the hosted, paid Web Read tool (mcp/web-read/server.json)."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ENTRY = json.loads((ROOT / "mcp" / "web-read" / "server.json").read_text())


def test_identity_is_in_this_repositorys_github_namespace() -> None:
    assert ENTRY["name"] == "io.github.domondi1/inferrail-web-read"
    assert ENTRY["repository"]["url"] == "https://github.com/domondi1/inferrail"


def test_description_fits_registry_limit() -> None:
    assert 0 < len(ENTRY["description"]) <= 100


def test_the_remote_is_a_streamable_http_server_at_web_reads_mcp_mount() -> None:
    (remote,) = ENTRY["remotes"]
    assert remote["type"] == "streamable-http"
    assert remote["url"].startswith("https://") and remote["url"].endswith("/read/mcp/")


def test_version_matches_what_the_server_reports() -> None:
    src = (ROOT / "hosted" / "web_read" / "mcp_tool.py").read_text()
    m = re.search(r'SERVER_VERSION = "([^"]+)"', src)
    assert m and m.group(1) == ENTRY["version"]


def test_the_local_package_is_the_published_npm_mcp_server_with_its_wallet_settings() -> None:
    pkg = json.loads((ROOT / "packages" / "ai-sdk-tools" / "package.json").read_text())
    (local,) = ENTRY["packages"]
    assert local["registryType"] == "npm" and local["transport"] == {"type": "stdio"}
    assert local["identifier"] == pkg["name"] and local["version"] == pkg["version"]
    assert pkg["mcpName"] == ENTRY["name"]  # the registry's npm ownership check
    assert list(pkg["bin"]) == ["inferrail-mcp"]  # `npx <package>` runs the only bin
    # Paying needs these without extra flags: required (not optional) peers, so npx installs them.
    assert {"@x402/fetch", "@x402/evm", "viem"} <= set(pkg["peerDependencies"])
    assert "peerDependenciesMeta" not in pkg
    env = {e["name"]: e for e in local["environmentVariables"]}
    assert env["INFERRAIL_WALLET_KEY"]["isSecret"] is True
    assert env["INFERRAIL_MAX_USD_PER_DAY"]["isSecret"] is False
