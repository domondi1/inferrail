"""Shared cases for the production database-path guard.

`hosted/work_economics/service.py` and `hosted/a2a_economic_authority/
server.py` each carry their own copy of `resolve_production_db_path` (each
hosted service is a standalone deployable). Both test files run every case
below against their service's copy, so the two cannot drift apart.

Also provides a server launcher for the production-shape subprocess tests.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

ENV_VAR = "SERVICE_DB_PATH"
ALLOW_VAR = "SERVICE_ALLOW_NEW_DB"

_TEMP_ROOTS = ("/tmp", "/var/tmp", "/dev/shm")


def is_under_temp(path: Path) -> bool:
    resolved = path.resolve()
    roots = {Path(r).resolve() for r in (*_TEMP_ROOTS, tempfile.gettempdir())}
    return any(resolved == r or resolved.is_relative_to(r) for r in roots)


@pytest.fixture(name="persistent_dir")
def persistent_dir_fixture() -> Iterator[Path]:
    """A writable directory that is NOT under any temporary location.

    pytest's `tmp_path` lives under the platform temp dir, which the guard
    rejects by design, so accepted-path cases need a directory elsewhere.
    Created under the user's home directory and removed afterwards.
    """
    path = Path(tempfile.mkdtemp(prefix=".inferrail-db-guard-test-", dir=Path.home()))
    try:
        assert not is_under_temp(path), (
            f"test precondition: {path} must not be under a temporary location"
        )
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@dataclass(frozen=True)
class Case:
    id: str
    # (persistent_dir, tmp_path) -> (raw value, environ)
    build: Callable[[Path, Path], tuple[str | None, dict[str, str]]]
    # None: accepted. Otherwise every substring must appear in the error.
    error: tuple[str, ...] | None


def _existing_file(d: Path, _t: Path) -> tuple[str, dict[str, str]]:
    f = d / "existing.sqlite3"
    f.write_bytes(b"")
    return str(f), {}


def _existing_dir(d: Path, _t: Path) -> tuple[str, dict[str, str]]:
    (d / "a-directory").mkdir()
    return str(d / "a-directory"), {}


def _symlink_into_temp(d: Path, t: Path) -> tuple[str, dict[str, str]]:
    link = d / "looks-persistent"
    link.symlink_to(t, target_is_directory=True)
    return str(link / "db.sqlite3"), {}


CASES: list[Case] = [
    Case("unset", lambda d, t: (None, {}), (ENV_VAR, "must be set")),
    Case("empty", lambda d, t: ("", {}), (ENV_VAR, "must be set")),
    Case("memory", lambda d, t: (":memory:", {}), ("in-memory",)),
    Case("memory-uri", lambda d, t: ("file::memory:?cache=shared", {}), ("in-memory",)),
    Case("tmp", lambda d, t: ("/tmp/wx.sqlite3", {ALLOW_VAR: "1"}), ("temporary storage",)),
    Case("var-tmp", lambda d, t: ("/var/tmp/wx.sqlite3", {ALLOW_VAR: "1"}), ("temporary",)),
    Case("dev-shm", lambda d, t: ("/dev/shm/wx.sqlite3", {ALLOW_VAR: "1"}), ("temporary",)),
    Case(
        "platform-tempdir",
        lambda d, t: (str(Path(tempfile.gettempdir()) / "wx.sqlite3"), {ALLOW_VAR: "1"}),
        ("temporary",),
    ),
    Case("pytest-tmp-path", lambda d, t: (str(t / "wx.sqlite3"), {ALLOW_VAR: "1"}), ("temporary",)),
    Case("symlink-into-temp", _symlink_into_temp, ("temporary",)),
    Case(
        "missing-parent",
        lambda d, t: (str(d / "no-such-dir" / "x.sqlite3"), {ALLOW_VAR: "1"}),
        ("parent directory", "does not exist"),
    ),
    Case("existing-file", _existing_file, None),
    Case("existing-directory", _existing_dir, ("not a regular file",)),
    Case(
        "new-file-without-opt-in",
        lambda d, t: (str(d / "new.sqlite3"), {}),
        ("does not exist", ALLOW_VAR),
    ),
    Case("new-file-with-opt-in", lambda d, t: (str(d / "new.sqlite3"), {ALLOW_VAR: "1"}), None),
    Case(
        "opt-in-must-be-exactly-1",
        lambda d, t: (str(d / "new.sqlite3"), {ALLOW_VAR: "true"}),
        ("does not exist", ALLOW_VAR),
    ),
    Case(
        "opt-in-zero-is-off",
        lambda d, t: (str(d / "new.sqlite3"), {ALLOW_VAR: "0"}),
        ("does not exist", ALLOW_VAR),
    ),
]


def check_case(resolve: Callable[..., Path], error_type: type[Exception], case: Case,
               persistent_dir: Path, tmp_path: Path) -> None:
    raw, environ = case.build(persistent_dir, tmp_path)
    if case.error is None:
        result = resolve(raw, env_var=ENV_VAR, allow_new_env_var=ALLOW_VAR, environ=environ)
        assert raw is not None
        assert result == Path(raw).resolve()
        assert result.is_absolute()
        if case.id == "new-file-with-opt-in":
            assert not result.exists(), "validation must never create the file itself"
        return
    with pytest.raises(error_type) as info:
        resolve(raw, env_var=ENV_VAR, allow_new_env_var=ALLOW_VAR, environ=environ)
    for fragment in case.error:
        assert fragment in str(info.value), f"{fragment!r} missing from: {info.value}"


# -- production-shape subprocess helpers --------------------------------------


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def run_until_exit(argv: list[str], env: dict[str, str], timeout: float = 60) -> (
    subprocess.CompletedProcess[str]
):
    return subprocess.run(
        [sys.executable, *argv], env=env, capture_output=True, text=True, timeout=timeout
    )


def starts_and_serves_health(
    argv: list[str], env: dict[str, str], port: int, timeout: float = 60
) -> tuple[bool, str]:
    """Starts the server, polls GET /health until 200 or timeout, stops it."""
    import httpx

    proc = subprocess.Popen(
        [sys.executable, *argv],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False, proc.stderr.read() if proc.stderr else ""
            try:
                if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).status_code == 200:
                    return True, ""
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        return False, "timed out waiting for /health"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def base_env() -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": str(Path.home())}
