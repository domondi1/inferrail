"""Check a repository's locked dependencies for known vulnerabilities with Inferrail Package Check.

    EVM_PRIVATE_KEY=0x... python check_deps.py --max-usd 0.02 [paths...]

Reads the lockfiles it finds (npm package-lock.json, Python requirements*.txt pins, poetry.lock,
uv.lock, Pipfile.lock), checks them 20 packages per call at $0.002 per call, paid with x402 from
the wallet whose key is in EVM_PRIVATE_KEY (a dedicated, low-balance Base wallet: it needs a
little USDC, no ETH). Before paying anything it works out the cost and stops if it is above
--max-usd; the x402 client also refuses any single offer above $0.002 before signing. Pages of
results are charged only when delivered, and an OSV outage is never charged.

Request ids come from the CI run (GITHUB_RUN_ID), so re-running a run within 10 minutes gets the
paid results back without paying again. Writes a job summary and annotations on GitHub Actions
and exits 1 when a package has a known vulnerability (unless --fail-on never).

Requires: pip install "x402[evm,httpx]==2.22.0" eth-account httpx
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import tomllib
import uuid
from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

ENDPOINT = "https://inferrail-cost-gateway.onrender.com/pkg/v1/check"
# Package Check's approved Base receiving address: the client pays no other address.
PAY_TO = "0xe9c1249648257C1C1D70F8b7682851C377bb8f53"
MAX_LOCKFILE_BYTES = 25_000_000
BASE_MAINNET = "eip155:8453"
PRICE_USD = Decimal("0.002")  # per call of up to BATCH packages
BATCH = 20
SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", "__pycache__"}
_PIN = re.compile(  # name[extras]==version, optionally followed by a marker or a comment
    r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?\s*==\s*([A-Za-z0-9.!+_-]+)\s*(?:[;#].*)?$"
)

Package = tuple[str, str, str]  # (OSV ecosystem, name, version)
# Lockfile contents are untrusted (a pull request can change them): only names and versions of
# these shapes are checked, and anything printed is escaped for the workflow-command syntax.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9@/._+-]{0,213}$")
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_!~-]{0,127}$")


def safe(pkg: Package) -> bool:
    return bool(_SAFE_NAME.match(pkg[1]) and _SAFE_VERSION.match(pkg[2]))


def escape(text: str) -> str:
    """Text for a GitHub workflow command, so it can't end the line or start another command."""
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def cell(text: str) -> str:
    """Text for a markdown table cell."""
    return re.sub(r"[\r\n|`<>\[\]]", " ", text)


def _npm(path: Path) -> set[Package]:
    data = json.loads(path.read_text())
    out: set[Package] = set()
    for key, info in (data.get("packages") or {}).items():  # lockfile v2/v3
        if not key or not isinstance(info, dict) or info.get("link"):
            continue  # the root project, or a workspace link
        name = info.get("name") or key.rsplit("node_modules/", 1)[-1]
        if isinstance(info.get("version"), str):
            out.add(("npm", name, info["version"]))
    if not out:  # lockfile v1
        stack = list((data.get("dependencies") or {}).items())
        while stack:
            name, info = stack.pop()
            if isinstance(info, dict) and isinstance(info.get("version"), str):
                out.add(("npm", name, info["version"]))
                stack.extend((info.get("dependencies") or {}).items())
    return out


def _requirements(path: Path) -> set[Package]:
    out: set[Package] = set()
    for line in path.read_text().splitlines():
        m = _PIN.match(line)
        if m:
            out.add(("PyPI", m.group(1), m.group(2)))
    return out


def _toml_packages(path: Path) -> set[Package]:  # poetry.lock and uv.lock: [[package]] tables
    data = tomllib.loads(path.read_text())
    return {("PyPI", p["name"], str(p["version"])) for p in data.get("package") or []
            if isinstance(p, dict) and p.get("name") and p.get("version")
            and (p.get("source") or {}).get("virtual") is None
            and (p.get("source") or {}).get("editable") is None}  # fmt: skip


def _pipfile(path: Path) -> set[Package]:
    data = json.loads(path.read_text())
    out: set[Package] = set()
    for section in ("default", "develop"):
        for name, info in (data.get(section) or {}).items():
            version = (info or {}).get("version", "") if isinstance(info, dict) else ""
            if version.startswith("=="):
                out.add(("PyPI", name, version[2:]))
    return out


PARSERS = {"package-lock.json": _npm, "poetry.lock": _toml_packages, "uv.lock": _toml_packages,
           "Pipfile.lock": _pipfile}  # fmt: skip


def _is_lockfile(name: str) -> bool:
    return name in PARSERS or re.fullmatch(r"requirements.*\.txt", name) is not None


def find_lockfiles(roots: Iterable[Path]) -> list[Path]:
    """Lockfiles under each root, never descending into vendored or hidden folders."""
    found: list[Path] = []
    for root in roots:
        if root.is_file():
            found.append(root)
            continue
        for here, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            found += [Path(here) / f for f in sorted(files) if _is_lockfile(f)]
    return found


def collect(lockfiles: Iterable[Path]) -> list[Package]:
    packages: set[Package] = set()
    for path in lockfiles:
        parse = PARSERS.get(path.name, _requirements)
        if path.stat().st_size > MAX_LOCKFILE_BYTES:
            print(f"::warning::skipped {escape(path.name)}: over {MAX_LOCKFILE_BYTES} bytes")
            continue
        try:
            packages |= parse(path)
        except (ValueError, KeyError, TypeError, AttributeError, tomllib.TOMLDecodeError) as exc:
            print(f"::warning::could not read {escape(str(path))}: {type(exc).__name__}")
    unsafe = {p for p in packages if not safe(p)}
    if unsafe:
        print(f"::warning::skipped {len(unsafe)} package entr(ies) with unexpected characters")
    return sorted(packages - unsafe)


def cost(n_packages: int) -> Decimal:
    return PRICE_USD * -(-n_packages // BATCH)


def _request_id(run_key: str, index: int, batch: list[Package]) -> str:
    digest = hashlib.sha256(json.dumps([run_key, index, batch]).encode()).hexdigest()[:32]
    return f"ci-{digest}"


class CheckNotBought(Exception):
    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status, self.body = status, body


async def check(
    packages: list[Package], account: Any, *, run_key: str, endpoint: str = ENDPOINT,
    network: str = BASE_MAINNET, pay_to: str = PAY_TO,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:  # fmt: skip
    """Every package's result, one paid call per batch of up to 20."""
    from x402 import x402Client
    from x402.http.clients.httpx import PaymentError, x402AsyncTransport
    from x402.mechanisms.evm.exact import ExactEvmScheme

    payer = x402Client()
    payer.register(network, ExactEvmScheme(signer=account))
    payer.set_spend_controls({"max_amount_per_payment": f"${PRICE_USD}"})
    # Only Package Check's own address on this network: an offer to anyone else is never signed.
    payer.register_policy(lambda _v, reqs: [
        r for r in reqs if str(getattr(r, "pay_to", "")).lower() == pay_to.lower()
        and str(getattr(r, "network", "")) == network])  # fmt: skip
    results: list[dict[str, Any]] = []
    async with httpx.AsyncClient(
        transport=x402AsyncTransport(payer, transport=transport), timeout=60,
        headers={"X-Inferrail-Source": "ci"},
    ) as http:  # fmt: skip
        for index in range(0, len(packages), BATCH):
            batch = packages[index : index + BATCH]
            body = {"packages": [{"ecosystem": e, "name": n, "version": v} for e, n, v in batch],
                    "request_id": _request_id(run_key, index, batch)}  # fmt: skip
            for attempt in (1, 2):
                try:
                    response = await http.post(endpoint, json=body)
                    break
                except (httpx.TransportError, PaymentError) as error:
                    lost = isinstance(error, httpx.TransportError) or isinstance(
                        error.__cause__, httpx.TransportError
                    )
                    if attempt == 2 or not lost:
                        raise
                    # Same packages and request_id from the same wallet: never charged twice.
            if response.status_code != 200:
                try:
                    detail: Any = response.json()
                except ValueError:
                    detail = response.text[:500]
                raise CheckNotBought(response.status_code, detail)
            results.extend(response.json()["results"])
    return results


def summary(results: list[dict[str, Any]], paid: Decimal) -> str:
    vulnerable = [r for r in results if r["status"] == "vulnerable"]
    incomplete = [r for r in results if r["status"] == "incomplete"]
    lines = [
        "## Dependency vulnerability check (Inferrail Package Check)", "",
        f"Checked **{len(results)}** packages for ${paid}: **{len(vulnerable)}** with known "
        f"vulnerabilities, {len(incomplete)} incomplete.", "",
    ]  # fmt: skip
    if vulnerable:
        lines += ["| Package | Version | Advisories | Fixed in |", "|---|---|---|---|"]
        for r in vulnerable:
            ids = ", ".join(cell(v["id"]) for v in r["vulns"][:5])
            fixed = ", ".join(cell(f) for f in sorted(
                {f for v in r["vulns"] for f in v.get("fixed", [])})[:3])  # fmt: skip
            lines.append(f"| {cell(r['ecosystem'])}:{cell(r['name'])} | {cell(r['version'])} "
                         f"| {ids} | {fixed} |")  # fmt: skip
        lines += ["", "Advisory links: https://osv.dev/vulnerability/<id>"]
        lines.append("")
    lines.append("No known vulnerabilities means OSV.dev had no matching advisory; it is not a "
                 "guarantee. Data: OSV.dev (advisories licensed per source).")  # fmt: skip
    return "\n".join(lines) + "\n"


def _github_out(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as f:
            f.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="*", default=["."], help="lockfiles or directories to scan")
    ap.add_argument("--max-usd", required=True, help="refuse to run if checking costs more")
    ap.add_argument("--on-missing-key", choices=["skip", "fail"], default="skip",
                    help="no wallet key (a pull request from a fork): skip or fail")  # fmt: skip
    ap.add_argument("--fail-on", choices=["vulnerable", "never"], default="vulnerable")
    ap.add_argument("--endpoint", default=ENDPOINT)
    ap.add_argument("--dry-run", action="store_true", help="list packages and cost; pay nothing")
    args = ap.parse_args(argv)
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request_target":
        # That event runs with the base repository's secrets on a pull request's changes: the
        # wallet key must never be exposed to it. Use pull_request (forks get no secrets).
        print("::error::refusing to run on pull_request_target (it would expose the wallet key "
              "to an untrusted pull request); use pull_request or push. Nothing was paid.")
        return 2
    lockfiles = find_lockfiles(Path(p) for p in args.paths)
    packages = collect(lockfiles)
    price = cost(len(packages))
    print(f"{len(packages)} packages in {len(lockfiles)} lockfile(s): {price} USD "
          f"({-(-len(packages) // BATCH)} call(s) of up to {BATCH})")  # fmt: skip
    _github_out("packages", str(len(packages)))
    if not packages:
        print("::notice::no lockfile with pinned packages found; nothing to check")
        return 0
    if price > Decimal(args.max_usd):
        print(f"::error::checking {len(packages)} packages costs ${price}, above max-usd "
              f"${args.max_usd}; nothing was paid. Raise max-usd or pass fewer paths.")  # fmt: skip
        return 2
    if args.dry_run:
        for e, n, v in packages:
            print(f"{e}:{n}@{v}")
        return 0
    key = os.environ.get("EVM_PRIVATE_KEY")
    if not key:
        level = "error" if args.on_missing_key == "fail" else "warning"
        print(f"::{level}::no wallet key (a pull request from a fork gets no secrets): the "
              "dependency check did not run and nothing was paid")  # fmt: skip
        return 2 if args.on_missing_key == "fail" else 0
    from eth_account import Account

    try:
        account = Account.from_key(key)
    except Exception:  # noqa: BLE001 - never echo the key or the library's message about it
        print("::error::the wallet key is not a valid private key; nothing was paid")
        return 2
    del key
    run_key = os.environ.get("GITHUB_RUN_ID") or uuid.uuid4().hex
    try:
        results = asyncio.run(check(packages, account, run_key=run_key,
                                    endpoint=args.endpoint))  # fmt: skip
    except CheckNotBought as exc:
        print(f"::error::Package Check refused the check (HTTP {exc.status}, not charged): "
              f"{escape(json.dumps(exc.body)[:300])}")  # fmt: skip
        return 2
    report = summary(results, price)
    print(report)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(report)
    vulnerable = [r for r in results if r["status"] == "vulnerable"]
    for r in vulnerable:
        ids = ", ".join(v["id"] for v in r["vulns"][:5])
        where = f"{r['ecosystem']}:{r['name']}@{r['version']}"
        print(f"::error title=Known vulnerability::{escape(where)}: {escape(ids)}")
    _github_out("vulnerable", str(len(vulnerable)))
    _github_out("paid-usd", str(price))
    return 1 if vulnerable and args.fail_on == "vulnerable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
