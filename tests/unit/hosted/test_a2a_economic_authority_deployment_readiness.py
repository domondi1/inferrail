"""Phase E readiness tests for hosted/a2a_economic_authority/'s deployment
surface: the `/health` liveness route and `main()`'s two invocation shapes
(explicit local/test args vs. production env-var-driven startup).

Deliberately does not start a real deployment -- these are local process
and unit-level checks only, proving the service *can* be deployed safely,
not deploying it. See `docs/capabilities/economic-authority.md`'s
"Deployment" section and `hosted/a2a_economic_authority/README.md`'s
"Deploying it" section for the design this verifies.

Skips automatically unless the hosted extra (a2a-sdk) is installed, same
as `test_a2a_economic_authority_transport.py`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402

pytest.importorskip("a2a")

import httpx  # noqa: E402
from _a2a_economic_authority_client import agent_process, free_port  # noqa: E402

EXAMPLES_DIR = REPO_ROOT / "examples"


@pytest.fixture
def paths(tmp_path: Path):
    return {
        "db": tmp_path / "authority.sqlite3",
        "cap_db": tmp_path / "capabilities.sqlite3",
    }


# -- /health -------------------------------------------------------------


def test_health_endpoint_returns_ok(paths):
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"]) as base_url:
        response = httpx.get(f"{base_url}/health", timeout=5.0)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


def test_health_endpoint_requires_no_credential(paths):
    """A deployment platform's health probe never presents a bearer
    credential -- this route must never require one."""
    port = free_port()
    with agent_process(port, paths["db"], paths["cap_db"]) as base_url:
        response = httpx.get(f"{base_url}/health", timeout=5.0, headers={})
        assert response.status_code == 200


# -- main() fails closed on missing configuration -------------------------
#
# Each case runs `server.py` as a real subprocess with a deliberately
# incomplete argv/environment and asserts it exits non-zero (argparse's
# `parser.error` -> `SystemExit(2)`) rather than starting with a broken or
# unreachable configuration. Real subprocesses, not a mocked argparse call,
# so this proves the actual CLI entry point's behavior.


def _run_server_briefly(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(HOSTED_DIR / "server.py"), *args],
        env=env,
        capture_output=True,
        text=True,
        # Generous: this fails before uvicorn ever starts, but the a2a-sdk/
        # fastapi/x402/cdp imports at module level still have to run first
        # (observed ~6s cold under normal load, more under CPU contention).
        timeout=30,
    )


def test_fails_closed_when_port_is_missing(tmp_path):
    env = {"PATH": os.environ["PATH"]}
    result = _run_server_briefly(
        [
            "--db-path",
            str(tmp_path / "a.sqlite3"),
            "--capability-db-path",
            str(tmp_path / "b.sqlite3"),
        ],
        env=env,
    )
    assert result.returncode != 0
    assert "--port" in result.stderr or "PORT" in result.stderr


def test_fails_closed_when_db_path_is_missing(tmp_path):
    env = {"PATH": os.environ["PATH"]}
    result = _run_server_briefly(
        ["--port", "0", "--capability-db-path", str(tmp_path / "b.sqlite3")],
        env=env,
    )
    assert result.returncode != 0
    assert "db-path" in result.stderr or "DB_PATH" in result.stderr


def test_fails_closed_when_capability_db_path_is_missing(tmp_path):
    env = {"PATH": os.environ["PATH"]}
    result = _run_server_briefly(
        ["--port", "0", "--db-path", str(tmp_path / "a.sqlite3")],
        env=env,
    )
    assert result.returncode != 0
    assert "capability-db-path" in result.stderr or "CAPABILITY_DB_PATH" in result.stderr


def test_fails_closed_in_production_shape_without_base_url(tmp_path):
    """The production shape (no CLI args at all) must refuse to start
    without an explicit public HTTPS base URL -- silently falling back to
    an internal bind address would make the Agent Card and x402 resource
    URL advertise an unreachable endpoint to real callers."""
    env = {
        **os.environ,
        "PORT": str(free_port()),
        "ECONOMIC_AUTHORITY_DB_PATH": str(tmp_path / "a.sqlite3"),
        "ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH": str(tmp_path / "b.sqlite3"),
    }
    env.pop("ECONOMIC_AUTHORITY_BASE_URL", None)
    result = _run_server_briefly([], env=env)
    assert result.returncode != 0
    assert "ECONOMIC_AUTHORITY_BASE_URL" in result.stderr


# -- credentials never appear in example output ----------------------------

# Identifiers that hold a plaintext bearer credential or plaintext recovery
# secret at some point in examples/economic_authority_session.py. None of
# these may ever be interpolated into a `print(...)` call -- a real user
# copy-pasting this example and running it against a real deployment must
# never have a credential land in their terminal scrollback or shell
# history/logging.
_SENSITIVE_IDENTIFIERS = {
    "root_token",
    "new_root_token",
    "child_token",
    "original_token",
    "authorizing_token",
    "recovery_secret",
    "token",
}


def test_example_never_prints_a_credential_or_recovery_secret():
    """Static check (parses the real AST, not a substring search) that no
    `print(...)` call in the example script interpolates a variable known
    to hold a plaintext credential or recovery secret, directly or via an
    f-string. Complements (does not replace) the transport-level proof
    that credentials never appear in server logs
    (`test_tokens_never_appear_in_task_history_or_logs` in
    `test_a2a_economic_authority_transport.py`) -- this one guards the
    *client-side* example instead."""
    import ast

    source = (EXAMPLES_DIR / "economic_authority_session.py").read_text()
    tree = ast.parse(source)

    def names_in(node: ast.AST) -> set[str]:
        return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}

    offenders: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
        ):
            used: set[str] = set()
            for arg in node.args:
                used |= names_in(arg)
            hit = used & _SENSITIVE_IDENTIFIERS
            if hit:
                offenders.append(f"line {node.lineno}: print() references {sorted(hit)}")

    assert not offenders, "example script prints a credential/secret:\n" + "\n".join(offenders)


# -- no multi-worker option -------------------------------------------------


def test_server_module_never_exposes_a_workers_flag():
    """`uvicorn.run(..., workers=N)` with N>1 would silently break the two
    in-memory stores (`InMemoryTaskStore`, `InMemoryCredentialHandoff`) --
    see server.py's module docstring, "Durability and single-process
    requirement". This must stay true at every future phase until those
    stores are made durable/shared.

    Parses the actual AST (not a substring search) so comments/docstrings
    that merely *mention* `--workers`/`workers=` while explaining this
    constraint don't produce a false failure.
    """
    import ast

    tree = ast.parse((HOSTED_DIR / "server.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "add_argument":
                for arg in node.args:
                    if isinstance(arg, ast.Constant) and arg.value == "--workers":
                        pytest.fail("server.py must never expose a --workers CLI flag")
            if isinstance(func, ast.Attribute) and func.attr == "run":
                for kw in node.keywords:
                    assert kw.arg != "workers", (
                        "server.py's uvicorn.run() must never pass workers="
                    )
