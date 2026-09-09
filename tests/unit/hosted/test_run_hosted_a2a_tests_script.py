"""Deterministic verification of scripts/run_hosted_a2a_tests.sh's exit-code
behavior (repair item 7): pytest's real failure must be authoritative
through the `pytest ... | tee` pipeline, and a skipped test must fail the
job even when pytest itself reports success. Runs the actual script
against small, synthetic, throwaway pytest files -- not the real Economic
Authority suite -- so this test needs no `a2a-sdk` and runs in every CI
job, not only the one that installs the hosted extra.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "run_hosted_a2a_tests.sh"


def _run_script(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_script_is_executable_and_uses_pipefail():
    source = SCRIPT.read_text()
    assert "set -euo pipefail" in source or "set -eo pipefail" in source, (
        "the script must explicitly enable pipefail so pytest's real exit "
        "code survives being piped through tee"
    )


def test_a_failing_pytest_invocation_returns_nonzero(tmp_path: Path):
    failing_test = tmp_path / "test_deliberately_failing.py"
    failing_test.write_text("def test_this_fails():\n    assert False\n")

    result = _run_script(failing_test)

    assert result.returncode != 0, (
        f"a failing pytest run must return nonzero through the pipeline; "
        f"got {result.returncode}\nstdout={result.stdout}\nstderr={result.stderr}"
    )


def test_a_skipped_test_fails_the_job_even_though_pytest_itself_succeeds(tmp_path: Path):
    skipped_test = tmp_path / "test_deliberately_skipped.py"
    skipped_test.write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='synthetic skip for repair item 7 verification')\n"
        "def test_this_is_skipped():\n"
        "    assert True\n"
    )

    result = _run_script(skipped_test)

    assert result.returncode != 0, (
        "a run with a skipped test must fail even though pytest's own exit "
        f"code would be 0\nstdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "skipped" in result.stderr.lower() or "SKIPPED" in result.stdout


def test_a_completely_passing_zero_skip_run_succeeds(tmp_path: Path):
    passing_test = tmp_path / "test_deliberately_passing.py"
    passing_test.write_text("def test_this_passes():\n    assert True\n")

    result = _run_script(passing_test)

    assert result.returncode == 0, (
        f"a fully passing, zero-skip run must succeed\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )


def test_no_argument_default_targets_the_real_economic_authority_suite():
    source = SCRIPT.read_text()
    assert "test_a2a_economic_authority_core.py" in source
    assert "test_a2a_economic_authority_capabilities.py" in source
    assert "test_a2a_economic_authority_sessions.py" in source
    assert "test_a2a_economic_authority_transport.py" in source


def test_ci_workflow_invokes_the_script_with_bash_shell():
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "run_hosted_a2a_tests.sh" in workflow
    assert "shell: bash" in workflow
