"""Deterministic verification of scripts/run_hosted_work_economics_tests.sh's
exit-code behavior: pytest's real failure must survive the `| tee`
pipeline, and a skipped test must fail the job even when pytest itself
succeeds. Runs the actual script against synthetic throwaway test files,
so it needs no hosted dependencies and runs in every CI job.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "run_hosted_work_economics_tests.sh"


def _run_script(target: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(SCRIPT), str(target)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_script_uses_pipefail():
    assert "set -euo pipefail" in SCRIPT.read_text()


def test_a_failing_pytest_invocation_returns_nonzero(tmp_path: Path):
    failing = tmp_path / "test_deliberately_failing.py"
    failing.write_text("def test_this_fails():\n    assert False\n")
    assert _run_script(failing).returncode != 0


def test_a_skipped_test_fails_the_job_even_though_pytest_itself_succeeds(tmp_path: Path):
    skipped = tmp_path / "test_deliberately_skipped.py"
    skipped.write_text(
        "import pytest\n\n"
        "@pytest.mark.skip(reason='synthetic skip')\n"
        "def test_this_is_skipped():\n"
        "    assert True\n"
    )
    result = _run_script(skipped)
    assert result.returncode != 0
    assert "skipped" in result.stderr.lower()


def test_a_module_level_importorskip_fails_the_job(tmp_path: Path):
    missing = tmp_path / "test_missing_dependency.py"
    missing.write_text(
        "import pytest\n\n"
        "pytest.importorskip('a_module_that_does_not_exist_anywhere')\n\n"
        "def test_never_runs():\n"
        "    assert True\n"
    )
    assert _run_script(missing).returncode != 0


def test_a_strict_xfail_that_unexpectedly_passes_fails_the_job(tmp_path: Path):
    xpass = tmp_path / "test_unexpected_pass.py"
    xpass.write_text(
        "import pytest\n\n"
        "@pytest.mark.xfail(strict=True, reason='synthetic known defect')\n"
        "def test_defect_fixed_without_removing_the_marker():\n"
        "    assert True\n"
    )
    assert _run_script(xpass).returncode != 0


def test_a_passing_run_with_strict_xfails_succeeds(tmp_path: Path):
    ok = tmp_path / "test_passing_with_known_defect.py"
    ok.write_text(
        "import pytest\n\n"
        "def test_this_passes():\n"
        "    assert True\n\n"
        "@pytest.mark.xfail(strict=True, reason='synthetic known defect')\n"
        "def test_known_defect():\n"
        "    assert False\n"
    )
    result = _run_script(ok)
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"


def test_default_targets_cover_the_paid_path_and_sdk_contract_suites():
    source = SCRIPT.read_text()
    assert "test_work_economics_paid_path.py" in source
    assert "test_x402_sdk_contract.py" in source


def test_ci_workflow_runs_the_script_with_bash_and_the_hosted_extra():
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert "run_hosted_work_economics_tests.sh" in workflow
    job = workflow.split("hosted-work-economics:", 1)[1].split("\n  ap-exceptions:", 1)[0]
    assert 'pip install -e ".[dev,hosted]"' in job
    assert "shell: bash" in job
