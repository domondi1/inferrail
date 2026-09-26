"""Keeps server.json (the MCP Registry entry) consistent with the package.

The registry accepts a publish only if the PyPI package's README carries
the same `mcp-name:` marker as server.json's `name`, and the command it
tells clients to run (`uvx inferrail mcp`) must exist in that package.
Schema validation itself runs in CI with the official `mcp-publisher
validate` (see .github/workflows/ci.yml); these checks cover what the
schema can't: agreement with pyproject.toml, README.md, and the CLI.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from inferrail_mcp.server import RECEIPTS_PATH_ENV

from inferrail.cli.main import _build_parser

_ROOT = Path(__file__).resolve().parents[2]


def _server_json() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((_ROOT / "server.json").read_text(encoding="utf-8"))
    return data


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_name_matches_readme_mcp_name_marker() -> None:
    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    # The registry requires the marker to end at a boundary (whitespace,
    # tag, or comment close), not glued to punctuation.
    markers = re.findall(r"mcp-name: (\S+?)(?=\s|<|-->|$)", readme)
    assert markers == [_server_json()["name"]]


def test_versions_match_pyproject() -> None:
    server = _server_json()
    version = _pyproject()["project"]["version"]
    assert server["version"] == version
    assert [p["version"] for p in server["packages"]] == [version]


def test_package_is_this_pypi_project_and_repo() -> None:
    server = _server_json()
    project = _pyproject()["project"]
    (package,) = server["packages"]
    assert package["registryType"] == "pypi"
    assert package["identifier"] == project["name"]
    assert server["repository"]["url"] == project["urls"]["Repository"]


def test_launch_command_exists_and_needs_no_extras() -> None:
    (package,) = _server_json()["packages"]
    args = [a["value"] for a in package["packageArguments"]]
    assert args == ["mcp"]
    assert package["transport"] == {"type": "stdio"}
    assert _build_parser().parse_args(args).command == "mcp"
    # `uvx inferrail mcp` installs no extras, so the SDK must be core.
    deps = _pyproject()["project"]["dependencies"]
    assert any(re.match(r"mcp\s*>=\s*2", d) for d in deps)


def test_declared_env_var_is_the_one_the_server_reads() -> None:
    (package,) = _server_json()["packages"]
    names = [e["name"] for e in package.get("environmentVariables", [])]
    assert names == [RECEIPTS_PATH_ENV]


def test_description_fits_registry_limit() -> None:
    assert 0 < len(_server_json()["description"]) <= 100


def test_registry_identity_and_launch_shape() -> None:
    server = _server_json()
    (package,) = server["packages"]
    assert server["name"] == "io.github.domondi1/inferrail"
    assert server["repository"] == {
        "url": "https://github.com/domondi1/inferrail",
        "source": "github",
    }
    assert package["runtimeHint"] == "uvx"
    (env_var,) = package["environmentVariables"]
    assert env_var["isRequired"] is False
    assert env_var["isSecret"] is False
