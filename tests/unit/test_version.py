"""Guards against the exact drift a prior audit found: pyproject.toml's
declared version and `inferrail.__version__` silently disagreeing. See
src/inferrail/__init__.py, which derives `__version__` from installed
package metadata specifically so this can't happen — this test is the CI
invariant that catches it if the derivation is ever changed back to a
hardcoded string.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import inferrail

_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"


def test_version_matches_pyproject_toml() -> None:
    pyproject = tomllib.loads(_PYPROJECT_PATH.read_text())
    assert inferrail.__version__ == pyproject["project"]["version"]


def test_fastapi_app_version_matches_package_version() -> None:
    import os

    from inferrail.config.quickstart import build_quickstart_config
    from inferrail.gateway.app import create_app

    os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-a-real-key")
    app = create_app(build_quickstart_config(telemetry_sink="none"))

    assert app.version == inferrail.__version__


def test_mcp_server_version_matches_package_version() -> None:
    from inferrail_mcp.server import server

    assert server.version == inferrail.__version__
