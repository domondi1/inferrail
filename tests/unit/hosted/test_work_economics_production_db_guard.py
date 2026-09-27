"""Production database-path guard for hosted/work_economics/service.py.

The production shape (`python3 service.py`, no CLI args) must never run
on ephemeral or unexpectedly fresh purchase storage. The explicit
local/test shape (`python3 service.py <db_path> <port>`) is unchanged and
still accepts any path. No payment, facilitator, or network call is
involved: rejections happen before the app is built, and the startup
checks only hit `/health`.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("cdp")
pytest.importorskip("x402")
pytest.importorskip("httpx")

HERE = Path(__file__).resolve().parent
HOSTED_DIR = HERE.parents[2] / "hosted" / "work_economics"
SERVICE = HOSTED_DIR / "service.py"
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

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

PAY_TO = "0x000000000000000000000000000000000000dEaD"


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOSTED_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("X402_SELLER_PAY_TO_ADDRESS", PAY_TO)
    _load("capability", "capability.py")
    _load("store", "store.py")
    return _load("work_economics_service_db_guard", "service.py")


# -- the guard function ------------------------------------------------------


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_guard_case(service: ModuleType, case: Case, persistent_dir: Path, tmp_path: Path):
    check_case(
        service.resolve_production_db_path,
        service.ProductionDbPathError,
        case,
        persistent_dir,
        tmp_path,
    )


def test_unwritable_parent_is_rejected(
    service: ModuleType, persistent_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    real_access = os.access
    monkeypatch.setattr(
        os, "access", lambda p, mode: False if Path(p) == persistent_dir else real_access(p, mode)
    )
    with pytest.raises(service.ProductionDbPathError, match="not writable"):
        service.resolve_production_db_path(
            str(persistent_dir / "db.sqlite3"),
            env_var="X",
            allow_new_env_var="Y",
            environ={"Y": "1"},
        )


def test_env_var_names(service: ModuleType):
    assert service.DB_PATH_ENV_VAR == "WORK_ECONOMICS_DB_PATH"
    assert service.ALLOW_NEW_DB_ENV_VAR == "WORK_ECONOMICS_ALLOW_NEW_DB"


def test_the_old_tmp_default_is_gone():
    assert "/tmp/inferrail_work_economics.sqlite3" not in SERVICE.read_text()


# -- the real entrypoint, production shape -------------------------------------


def _prod_env(**extra: str) -> dict[str, str]:
    env = {
        **base_env(),
        "X402_SELLER_PAY_TO_ADDRESS": PAY_TO,
        "CDP_API_KEY_ID": "unused-in-tests",
        "CDP_API_KEY_SECRET": "unused-in-tests",
    }
    env.update(extra)
    return env


def test_production_shape_refuses_without_an_explicit_db_path():
    legacy_default = Path("/tmp/inferrail_work_economics.sqlite3")
    existed_before = legacy_default.exists()
    result = run_until_exit([str(SERVICE)], _prod_env(PORT=str(free_port())))
    assert result.returncode != 0
    assert "refusing to start" in result.stderr
    assert "WORK_ECONOMICS_DB_PATH" in result.stderr
    if not existed_before:
        assert not legacy_default.exists(), "must not fall back to the old /tmp default"


def test_production_shape_refuses_a_temporary_path(tmp_path: Path):
    db = tmp_path / "purchases.sqlite3"
    result = run_until_exit(
        [str(SERVICE)],
        _prod_env(
            PORT=str(free_port()),
            WORK_ECONOMICS_DB_PATH=str(db),
            WORK_ECONOMICS_ALLOW_NEW_DB="1",
        ),
    )
    assert result.returncode != 0
    assert "temporary storage" in result.stderr
    assert not db.exists()


def test_production_shape_refuses_a_fresh_db_without_opt_in(persistent_dir: Path):
    db = persistent_dir / "purchases.sqlite3"
    result = run_until_exit(
        [str(SERVICE)], _prod_env(PORT=str(free_port()), WORK_ECONOMICS_DB_PATH=str(db))
    )
    assert result.returncode != 0
    assert "WORK_ECONOMICS_ALLOW_NEW_DB" in result.stderr
    assert not db.exists()


def test_production_shape_starts_with_opt_in_then_without_it_once_the_db_exists(
    persistent_dir: Path,
):
    db = persistent_dir / "purchases.sqlite3"

    port = free_port()
    ok, err = starts_and_serves_health(
        [str(SERVICE)],
        _prod_env(PORT=str(port), WORK_ECONOMICS_DB_PATH=str(db), WORK_ECONOMICS_ALLOW_NEW_DB="1"),
        port,
    )
    assert ok, err
    assert db.is_file()

    port = free_port()
    ok, err = starts_and_serves_health(
        [str(SERVICE)], _prod_env(PORT=str(port), WORK_ECONOMICS_DB_PATH=str(db)), port
    )
    assert ok, err


def test_explicit_local_shape_still_accepts_a_temporary_path(tmp_path: Path):
    db = tmp_path / "purchases.sqlite3"
    port = free_port()
    ok, err = starts_and_serves_health([str(SERVICE), str(db), str(port)], _prod_env(), port)
    assert ok, err
    assert db.is_file()
