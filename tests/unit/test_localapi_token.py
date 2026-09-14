from __future__ import annotations

import stat
import sys
from pathlib import Path

import pytest

from inferrail.localapi.token import ensure_local_api_token


def test_generates_and_persists_a_token(tmp_path: Path) -> None:
    token_path = tmp_path / "local-api-token"

    token = ensure_local_api_token(token_path)

    assert token_path.exists()
    assert token_path.read_text(encoding="utf-8").strip() == token
    assert len(token) > 20


def test_second_call_returns_the_same_token(tmp_path: Path) -> None:
    token_path = tmp_path / "local-api-token"

    first = ensure_local_api_token(token_path)
    second = ensure_local_api_token(token_path)

    assert first == second


def test_creates_parent_directories(tmp_path: Path) -> None:
    token_path = tmp_path / "nested" / "dir" / "local-api-token"

    ensure_local_api_token(token_path)

    assert token_path.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file mode bits don't apply on Windows")
def test_token_file_is_owner_only_permissions(tmp_path: Path) -> None:
    token_path = tmp_path / "local-api-token"

    ensure_local_api_token(token_path)

    mode = stat.S_IMODE(token_path.stat().st_mode)
    assert mode == 0o600


def test_lost_creation_race_reads_back_the_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    token_path = tmp_path / "local-api-token"
    token_path.parent.mkdir(parents=True, exist_ok=True)
    # The file already exists (another process won), but we force the
    # early `.exists()` check to miss it anyway, so the test actually
    # exercises the O_EXCL race branch rather than the early return.
    token_path.write_text("winner-token", encoding="utf-8")
    original_exists = Path.exists

    def _fake_exists(self: Path, *a: object, **kw: object) -> bool:
        if self == token_path:
            return False
        return original_exists(self, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "exists", _fake_exists)

    def _raising_open(path: object, flags: int, mode: int = 0o777) -> int:
        if flags & os.O_EXCL:
            raise FileExistsError("simulated race: another process created it first")
        raise AssertionError("should not reach a non-O_EXCL open in this test")

    monkeypatch.setattr(os, "open", _raising_open)

    result = ensure_local_api_token(token_path)

    assert result == "winner-token"
