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


def test_it_is_a_remote_only_streamable_http_server_at_web_reads_mcp_mount() -> None:
    assert "packages" not in ENTRY
    (remote,) = ENTRY["remotes"]
    assert remote["type"] == "streamable-http"
    assert remote["url"].startswith("https://") and remote["url"].endswith("/read/mcp/")


def test_version_matches_what_the_server_reports() -> None:
    src = (ROOT / "hosted" / "web_read" / "mcp_tool.py").read_text()
    m = re.search(r'SERVER_VERSION = "([^"]+)"', src)
    assert m and m.group(1) == ENTRY["version"]
