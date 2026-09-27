"""Production database-path guard for hosted/a2a_economic_authority/server.py.

The production shape (`python3 server.py`, no --port/--db-path/
--capability-db-path) must never run either SQLite database on ephemeral
or unexpectedly fresh storage. The explicit local/test shape is unchanged
and still accepts any path. Session purchase is not enabled in these
startups (no pay-to address is set), so no payment or facilitator is
involved; the startup checks only hit `/health`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("a2a")
pytest.importorskip("cdp")
pytest.importorskip("x402")
pytest.importorskip("httpx")

HERE = Path(__file__).resolve().parent
HOSTED_DIR = HERE.parents[2] / "hosted" / "a2a_economic_authority"
SERVER = HOSTED_DIR / "server.py"
for p in (HOSTED_DIR, HERE):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import server  # noqa: E402
from _production_db_guard_cases import (  # noqa: E402
    CASES,
    Case,
    base_env,
    check_case,
    free_port,
    persistent_dir_fixture,  # noqa: F401  (registers the persistent_dir fixture)
    run_until_exit,
    starts_and_serves_health,
)


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_guard_case(case: Case, persistent_dir: Path, tmp_path: Path):
    check_case(
        server.resolve_production_db_path,
        server.ProductionDbPathError,
        case,
        persistent_dir,
        tmp_path,
    )


def test_allow_new_db_env_var_name():
    assert server.ALLOW_NEW_DB_ENV_VAR == "ECONOMIC_AUTHORITY_ALLOW_NEW_DB"


# -- the real entrypoint, production shape -------------------------------------


def _prod_env(port: int, db: Path, cap: Path, **extra: str) -> dict[str, str]:
    env = {
        **base_env(),
        "PORT": str(port),
        "ECONOMIC_AUTHORITY_BASE_URL": "https://authority.example.test/",
        "ECONOMIC_AUTHORITY_DB_PATH": str(db),
        "ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH": str(cap),
    }
    env.update(extra)
    return env


def test_production_shape_refuses_a_temporary_db_path(tmp_path: Path, persistent_dir: Path):
    db, cap = tmp_path / "authority.sqlite3", persistent_dir / "capabilities.sqlite3"
    result = run_until_exit(
        [str(SERVER)],
        _prod_env(free_port(), db, cap, ECONOMIC_AUTHORITY_ALLOW_NEW_DB="1"),
    )
    assert result.returncode != 0
    assert "ECONOMIC_AUTHORITY_DB_PATH" in result.stderr
    assert "temporary storage" in result.stderr
    assert not db.exists() and not cap.exists()


def test_production_shape_refuses_a_temporary_capability_db_path(
    tmp_path: Path, persistent_dir: Path
):
    db, cap = persistent_dir / "authority.sqlite3", tmp_path / "capabilities.sqlite3"
    result = run_until_exit(
        [str(SERVER)],
        _prod_env(free_port(), db, cap, ECONOMIC_AUTHORITY_ALLOW_NEW_DB="1"),
    )
    assert result.returncode != 0
    assert "ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH" in result.stderr
    assert "temporary storage" in result.stderr
    assert not db.exists() and not cap.exists()


def test_production_shape_refuses_fresh_dbs_without_opt_in(persistent_dir: Path):
    db, cap = persistent_dir / "authority.sqlite3", persistent_dir / "capabilities.sqlite3"
    result = run_until_exit([str(SERVER)], _prod_env(free_port(), db, cap))
    assert result.returncode != 0
    assert "ECONOMIC_AUTHORITY_ALLOW_NEW_DB" in result.stderr
    assert not db.exists() and not cap.exists()


def test_production_shape_refuses_when_only_one_db_survived(persistent_dir: Path):
    """One file present and the other missing looks like a partial reset."""
    db, cap = persistent_dir / "authority.sqlite3", persistent_dir / "capabilities.sqlite3"
    db.write_bytes(b"")
    result = run_until_exit([str(SERVER)], _prod_env(free_port(), db, cap))
    assert result.returncode != 0
    assert "ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH" in result.stderr
    assert not cap.exists()


def test_base_url_is_still_reported_before_the_db_guard(tmp_path: Path):
    env = _prod_env(free_port(), tmp_path / "a.sqlite3", tmp_path / "b.sqlite3")
    env.pop("ECONOMIC_AUTHORITY_BASE_URL")
    result = run_until_exit([str(SERVER)], env)
    assert result.returncode != 0
    assert "ECONOMIC_AUTHORITY_BASE_URL" in result.stderr
    assert "temporary storage" not in result.stderr


def test_production_shape_starts_with_opt_in_then_without_it_once_dbs_exist(
    persistent_dir: Path,
):
    db, cap = persistent_dir / "authority.sqlite3", persistent_dir / "capabilities.sqlite3"

    port = free_port()
    ok, err = starts_and_serves_health(
        [str(SERVER)], _prod_env(port, db, cap, ECONOMIC_AUTHORITY_ALLOW_NEW_DB="1"), port
    )
    assert ok, err
    assert db.is_file() and cap.is_file()

    port = free_port()
    ok, err = starts_and_serves_health([str(SERVER)], _prod_env(port, db, cap), port)
    assert ok, err


def test_explicit_local_shape_still_accepts_temporary_paths(tmp_path: Path):
    db, cap = tmp_path / "authority.sqlite3", tmp_path / "capabilities.sqlite3"
    port = free_port()
    ok, err = starts_and_serves_health(
        [
            str(SERVER),
            "--port", str(port),
            "--db-path", str(db),
            "--capability-db-path", str(cap),
        ],
        base_env(),
        port,
    )
    assert ok, err
    assert db.is_file() and cap.is_file()
