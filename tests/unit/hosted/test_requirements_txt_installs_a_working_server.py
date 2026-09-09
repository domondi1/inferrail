"""Regression test for the packaging gap `hosted/a2a_economic_authority/`'s
own dev-extra install (`pip install -e ".[dev,hosted]"`) could never catch:
`server.py` imports `cdp.x402` and several `x402.*` modules unconditionally
at module load (Phase C's paid-session wiring), not lazily behind
`ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS` -- so a real deployment
building from `hosted/a2a_economic_authority/requirements.txt` alone (the
file `hosted/a2a_economic_authority/README.md`'s "Deploying it" section
tells an operator to run `pip install -r requirements.txt` against) failed
to import at all, even in a Phase-B-only deployment that never sets that
variable. Every other test in this directory runs against this repo's own
environment, which always has the full `[dev,hosted]` extra installed --
none of them could have caught a `requirements.txt` that quietly drifted
out of sync with what `server.py` actually imports.

Builds one real, isolated venv (module-scoped fixture, built once and
reused by both tests below) and installs *only*
`hosted/a2a_economic_authority/requirements.txt` into it -- nothing else
from this repo's environment leaks in, since `python -m venv` a fresh
interpreter has no access to packages installed elsewhere. Proves both
that `server.py` imports cleanly in that isolated environment and that it
reaches its real fail-closed startup behavior there (not just that
`import server` doesn't raise) -- see
`tests/unit/hosted/test_a2a_economic_authority_deployment_readiness.py`
for the equivalent fail-closed checks run against *this* repo's own
environment instead.

Skips automatically unless the hosted extra (a2a-sdk) is installed in the
*current* interpreter -- same gate every other hosted-specific test in
this directory uses, so this stays confined to the dedicated
`hosted-economic-authority` CI job rather than slowing down the main
`test` job. Builds a real venv and does a real network pip install, so
this is slow (well over a minute) by design -- that cost buys a guarantee
none of the other tests in this repo can provide.
"""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
HOSTED_DIR = REPO_ROOT / "hosted" / "a2a_economic_authority"
REQUIREMENTS = HOSTED_DIR / "requirements.txt"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

import pytest  # noqa: E402

pytest.importorskip("a2a")


@pytest.fixture(scope="module")
def clean_venv_python(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Builds one throwaway venv with only `requirements.txt` installed,
    shared by every test in this module -- venv creation plus a real pip
    install of cdp-sdk/x402/a2a-sdk/fastapi is expensive enough (well over
    a minute) that rebuilding it per test would be wasteful for no extra
    safety."""
    venv_dir = tmp_path_factory.mktemp("economic_authority_clean_venv")
    venv.create(venv_dir, with_pip=True)
    venv_python = str(venv_dir / "bin" / "python")

    install = subprocess.run(
        [venv_python, "-m", "pip", "install", "--quiet", "-r", str(REQUIREMENTS)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert install.returncode == 0, (
        f"pip install -r requirements.txt failed in a clean venv:\n{install.stderr}"
    )
    return venv_python


def test_requirements_txt_alone_lets_server_import(clean_venv_python: str) -> None:
    """The exact bug this file exists to catch: `server.py` must import
    cleanly using *only* what `requirements.txt` installs -- no reliance
    on this repo's own `[dev,hosted]` extra, which every other test in
    this directory runs under."""
    result = subprocess.run(
        [clean_venv_python, "-c", "import server"],
        cwd=HOSTED_DIR,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"server.py failed to import with only requirements.txt installed:\n{result.stderr}"
    )


def test_requirements_txt_alone_reaches_fail_closed_startup(
    clean_venv_python: str, tmp_path: Path
) -> None:
    """Beyond importing, the isolated install must reach `main()`'s real
    fail-closed validation (a missing `--db-path` refuses to start with a
    clear error) -- proving the isolated environment supports the actual
    startup path a Render deployment runs, not just a bare `import`."""
    result = subprocess.run(
        [
            clean_venv_python,
            str(HOSTED_DIR / "server.py"),
            "--port",
            "0",
            "--capability-db-path",
            str(tmp_path / "capabilities.sqlite3"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 2
    assert "db-path" in result.stderr or "DB_PATH" in result.stderr
