"""actions/package-check: Package Check in a CI pipeline, paid from a developer-set wallet.

Proves the lockfile readers (npm, requirements pins, poetry/uv, Pipfile), that the cost cap is
enforced before anything is paid, and, against a real Package Check app (fake facilitator, fake
OSV, no funds), one paid call per 20 packages, the summary and outputs, a failing exit on known
vulnerabilities, and that re-running the same CI run recovers the paid results for free."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import sys
from pathlib import Path

import httpx
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
ACTION = ROOT / "actions" / "package-check"
spec = importlib.util.spec_from_file_location("ci_check_deps", ACTION / "check_deps.py")
assert spec is not None and spec.loader is not None
ci = importlib.util.module_from_spec(spec)
sys.modules["ci_check_deps"] = ci
spec.loader.exec_module(ci)


def _write(tmp: Path, name: str, text: str) -> Path:
    path = tmp / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


NPM_V3 = {"lockfileVersion": 3, "packages": {
    "": {"name": "app", "version": "1.0.0"},
    "node_modules/lodash": {"version": "4.17.15"},
    "node_modules/@scope/pkg": {"version": "2.0.0"},
    "node_modules/a/node_modules/lodash": {"version": "4.17.21"},
    "packages/local": {"link": True},
}}  # fmt: skip
NPM_V1 = {"lockfileVersion": 1, "dependencies": {
    "lodash": {"version": "4.17.15", "dependencies": {"minimist": {"version": "0.0.8"}}}}}


def test_npm_lockfiles_v1_and_v3(tmp_path):
    v3 = ci._npm(_write(tmp_path, "a/package-lock.json", json.dumps(NPM_V3)))
    assert v3 == {("npm", "lodash", "4.17.15"), ("npm", "@scope/pkg", "2.0.0"),
                  ("npm", "lodash", "4.17.21")}  # fmt: skip
    v1 = ci._npm(_write(tmp_path, "b/package-lock.json", json.dumps(NPM_V1)))
    assert v1 == {("npm", "lodash", "4.17.15"), ("npm", "minimist", "0.0.8")}


def test_only_exact_python_pins_are_checked(tmp_path):
    path = _write(tmp_path, "requirements.txt", "\n".join([
        "requests==2.32.3", "Django == 4.2.1  # pinned", "uvicorn[standard]==0.30.0",
        "flask>=2.0", "numpy", "-r other.txt", "attrs==23.2.0 ; python_version >= '3.8'",
        "# comment"]))  # fmt: skip
    assert ci._requirements(path) == {("PyPI", "requests", "2.32.3"), ("PyPI", "Django", "4.2.1"),
                                      ("PyPI", "uvicorn", "0.30.0"),
                                      ("PyPI", "attrs", "23.2.0")}  # fmt: skip


def test_poetry_uv_and_pipfile_locks(tmp_path):
    poetry = _write(tmp_path, "poetry.lock", '[[package]]\nname = "requests"\nversion = "2.31.0"\n'
                    '[[package]]\nname = "idna"\nversion = "3.6"\n')  # fmt: skip
    uv = _write(tmp_path, "uv.lock", '[[package]]\nname = "app"\nversion = "0.1.0"\n'
                'source = { editable = "." }\n[[package]]\nname = "httpx"\nversion = "0.27.0"\n')
    pipfile = _write(tmp_path, "Pipfile.lock", json.dumps(
        {"default": {"six": {"version": "==1.16.0"}}, "develop": {"pytest": {"version": "*"}}}))
    assert ci._toml_packages(poetry) == {("PyPI", "requests", "2.31.0"), ("PyPI", "idna", "3.6")}
    assert ci._toml_packages(uv) == {("PyPI", "httpx", "0.27.0")}  # not the project itself
    assert ci._pipfile(pipfile) == {("PyPI", "six", "1.16.0")}


def test_lockfiles_inside_vendored_folders_are_ignored(tmp_path):
    _write(tmp_path, "package-lock.json", json.dumps(NPM_V1))
    _write(tmp_path, "node_modules/x/package-lock.json", json.dumps(NPM_V1))
    _write(tmp_path, ".venv/requirements.txt", "a==1\n")
    _write(tmp_path, "svc/requirements-dev.txt", "b==2\n")
    found = [p.relative_to(tmp_path).as_posix() for p in ci.find_lockfiles([tmp_path])]
    assert found == ["package-lock.json", "svc/requirements-dev.txt"]


def test_an_unreadable_lockfile_is_a_warning_not_a_crash(tmp_path, capsys):
    bad = _write(tmp_path, "package-lock.json", "{not json")
    assert ci.collect([bad]) == []
    assert "::warning" in capsys.readouterr().out


@pytest.mark.parametrize(("n", "usd"), [(1, "0.002"), (20, "0.002"), (21, "0.004"),
                                        (250, "0.026")])  # fmt: skip
def test_cost_is_one_call_per_twenty_packages(n, usd):
    assert str(ci.cost(n)) == usd


def test_over_the_cap_nothing_is_paid(tmp_path, monkeypatch, capsys):
    lines = "\n".join(f"pkg{i}==1.0.{i}" for i in range(45))  # 3 calls = $0.006
    _write(tmp_path, "requirements.txt", lines)
    monkeypatch.setenv("EVM_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setattr(ci, "check", lambda *a, **k: pytest.fail("must not pay"))
    assert ci.main([str(tmp_path), "--max-usd", "0.004"]) == 2
    assert "nothing was paid" in capsys.readouterr().out


def test_a_dry_run_lists_packages_and_cost_without_a_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("EVM_PRIVATE_KEY", raising=False)
    _write(tmp_path, "requirements.txt", "requests==2.32.3\n")
    assert ci.main([str(tmp_path), "--dry-run", "--max-usd", "0.01"]) == 0
    assert "PyPI:requests@2.32.3" in capsys.readouterr().out


@pytest.mark.parametrize(("mode", "code"), [("skip", 0), ("fail", 2)])
def test_no_key_means_no_payment(tmp_path, monkeypatch, capsys, mode, code):
    # A pull request from a fork gets no secrets: skip (warn) or fail, never pay.
    monkeypatch.delenv("EVM_PRIVATE_KEY", raising=False)
    monkeypatch.setattr(ci, "check", lambda *a, **k: pytest.fail("must not pay"))
    _write(tmp_path, "requirements.txt", "requests==2.32.3\n")
    assert ci.main([str(tmp_path), "--max-usd", "0.01", "--on-missing-key", mode]) == code
    assert "nothing was paid" in capsys.readouterr().out


def test_the_spending_limit_is_required(tmp_path):
    with pytest.raises(SystemExit):
        ci.main([str(tmp_path)])


def test_pull_request_target_is_refused_before_anything(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request_target")
    monkeypatch.setenv("EVM_PRIVATE_KEY", "0x" + "1" * 64)
    monkeypatch.setattr(ci, "check", lambda *a, **k: pytest.fail("must not pay"))
    monkeypatch.setattr(ci, "find_lockfiles", lambda *a: pytest.fail("must not read the PR"))
    assert ci.main([str(tmp_path), "--max-usd", "1"]) == 2
    assert "pull_request_target" in capsys.readouterr().out


def test_a_malformed_key_is_never_echoed(tmp_path, monkeypatch, capsys):
    secret = "0xnot-a-key-" + "s3cr3t" * 5
    monkeypatch.setenv("EVM_PRIVATE_KEY", secret)
    monkeypatch.setattr(ci, "check", lambda *a, **k: pytest.fail("must not pay"))
    _write(tmp_path, "requirements.txt", "requests==2.32.3\n")
    assert ci.main([str(tmp_path), "--max-usd", "0.01"]) == 2
    out = capsys.readouterr()
    assert "s3cr3t" not in out.out + out.err and "not a valid private key" in out.out


def test_hostile_package_names_are_dropped_and_output_is_escaped(tmp_path, capsys):
    lock = {"packages": {
        "node_modules/ok": {"version": "1.0.0"},
        "node_modules/evil": {"name": "evil\n::add-mask::x", "version": "1.0.0"},
        "node_modules/evil2": {"version": "1.0.0%0A::stop-commands::"},
    }}  # fmt: skip
    path = _write(tmp_path, "package-lock.json", json.dumps(lock))
    assert ci.collect([path]) == [("npm", "ok", "1.0.0")]
    assert "skipped 2" in capsys.readouterr().out
    assert ci.escape("a\n::x%") == "a%0A::x%25" and "\n" not in ci.escape("x\r\ny")
    assert "|" not in ci.cell("a|b`c\nd")


def test_an_oversized_lockfile_is_skipped(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(ci, "MAX_LOCKFILE_BYTES", 10)
    path = _write(tmp_path, "requirements.txt", "requests==2.32.3\n")
    assert ci.collect([path]) == [] and "skipped" in capsys.readouterr().out


def test_the_action_keeps_the_key_out_of_the_command_text_and_installs_only_locked_code():
    action = yaml.safe_load((ACTION / "action.yml").read_text())
    assert action["runs"]["using"] == "composite" and action["inputs"]["wallet-key"]["required"]
    steps = action["runs"]["steps"]
    script = "\n".join(s.get("run", "") for s in steps)
    assert "inputs." not in script  # inputs reach the script only through env
    env = next(s["env"] for s in steps if s.get("id") == "check")
    assert env["EVM_PRIVATE_KEY"] == "${{ inputs.wallet-key }}"
    assert "--require-hashes" in script and "--no-deps" in script and "-I " in script
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", s["uses"])
               for s in steps if "uses" in s)  # third-party actions pinned to a commit
    limit = action["inputs"]["max-usd"]
    assert limit["required"] and "default" not in limit  # an explicit owner-set limit
    install = next(s for s in steps if "--require-hashes" in s.get("run", ""))
    assert "env" not in install  # the key isn't in the environment while installing


def test_every_locked_requirement_has_a_hash():
    lines = (ACTION / "requirements.txt").read_text().replace("\\\n", " ").splitlines()
    reqs = [line for line in lines if line and not line.lstrip().startswith("#")]
    assert len(reqs) > 10 and all("==" in r and "--hash=sha256:" in r for r in reqs)
    assert any(r.startswith("x402==2.22.0") for r in reqs)


# -- end to end against a real Package Check app -------------------------------------------------
pytest.importorskip("x402")
pytest.importorskip("eth_account")
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import FakeFacilitator  # noqa: E402
from eth_account import Account  # noqa: E402
from test_package_check import LODASH, SECRET, packages  # noqa: E402
from test_web_read import svc  # noqa: E402

TESTNET = svc.TESTNET
SELLER = "0x" + "6" * 40


def _app(tmp_path: Path, fac: FakeFacilitator, n: int):
    """Package Check over a fake OSV that answers every batch query, lodash 4.17.15 vulnerable."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        if request.url.path == "/v1/querybatch":
            queries = json.loads(request.content)["queries"]
            return httpx.Response(200, json={"results": [
                {"vulns": [{"id": "GHSA-35jh-r3h4-6jhm"}]}
                if q["package"]["name"] == "lodash" and q["version"] == "4.17.15" else {}
                for q in queries]})  # fmt: skip
        return httpx.Response(200, json=LODASH)

    osv = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = svc.Settings(pay_to=SELLER, resource_url="http://testserver/v1/check",
                            data_dir=tmp_path)  # fmt: skip
    app = svc.create_app(settings, facilitator=fac,
                         task=packages.package_task(svc, SECRET, client=osv))  # fmt: skip
    return httpx.ASGITransport(app=app), seen


def test_a_ci_run_pays_once_per_batch_and_reports_vulnerabilities(tmp_path):
    fac = FakeFacilitator()
    pkgs = [("npm", "lodash", "4.17.15")] + [("PyPI", f"p{i}", "1.0") for i in range(24)]  # 25
    transport, _ = _app(tmp_path, fac, 25)
    results = asyncio.run(ci.check(pkgs, Account.create(), run_key="run-1",
                                   endpoint="http://testserver/v1/check", pay_to=SELLER,
                                   network=TESTNET, transport=transport))  # fmt: skip
    assert len(results) == 25 and len(fac.calls_to("settle")) == 2  # 20 + 5
    report = ci.summary(results, ci.cost(25))
    assert "npm:lodash | 4.17.15" in report and "GHSA-35jh-r3h4-6jhm" in report
    assert "not a guarantee" in report


def test_re_running_the_same_ci_run_is_not_charged_again(tmp_path):
    fac = FakeFacilitator()
    buyer = Account.create()
    pkgs = [("npm", "lodash", "4.17.15")]
    transport, seen = _app(tmp_path, fac, 1)
    for _ in range(2):  # a re-run of the same workflow run
        asyncio.run(ci.check(pkgs, buyer, run_key="run-7", endpoint="http://testserver/v1/check",
                             pay_to=SELLER, network=TESTNET, transport=transport))  # fmt: skip
    assert len(fac.calls_to("settle")) == 1 and seen.count("POST /v1/querybatch") == 1


def test_main_writes_the_summary_and_outputs_and_fails_on_vulnerabilities(
    tmp_path, monkeypatch
):
    fac = FakeFacilitator()
    transport, _ = _app(tmp_path / "svc", fac, 1)
    repo = tmp_path / "repo"
    _write(repo, "package-lock.json", json.dumps({"packages": {
        "node_modules/lodash": {"version": "4.17.15"}}}))  # fmt: skip
    summary, outputs = tmp_path / "summary.md", tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setenv("GITHUB_OUTPUT", str(outputs))
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("EVM_PRIVATE_KEY", Account.create().key.hex())
    real = ci.check

    async def via_test_app(*a, **k):
        local = {"endpoint": "http://testserver/v1/check", "network": TESTNET,
                 "pay_to": SELLER, "transport": transport}
        return await real(*a, **{**k, **local})

    monkeypatch.setattr(ci, "check", via_test_app)
    assert ci.main([str(repo), "--max-usd", "0.01"]) == 1  # a known vulnerability fails the step
    assert "npm:lodash" in summary.read_text()
    out = outputs.read_text()
    assert "packages=1" in out and "vulnerable=1" in out and "paid-usd=0.002" in out
    assert ci.main([str(repo), "--max-usd", "0.01", "--fail-on", "never"]) == 0


def test_an_offer_to_any_other_address_is_never_signed(tmp_path):
    fac = FakeFacilitator()
    transport, seen = _app(tmp_path, fac, 1)  # this test seller is not Package Check's address
    with pytest.raises(Exception, match="(?i)no payment requirements|filtered"):
        asyncio.run(ci.check([("npm", "lodash", "4.17.15")], Account.create(), run_key="r",
                             endpoint="http://testserver/v1/check", network=TESTNET,
                             transport=transport))  # default pay_to = Package Check's  # fmt: skip
    assert fac.calls_to("settle") == [] and fac.calls_to("verify") == []
    assert "POST /v1/querybatch" not in seen
