"""Inferrail Package Check: known vulnerabilities for the package versions an agent is about to use.

`POST /v1/check {"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}, ...]}`
checks up to 20 package versions in one call against OSV.dev and returns, per package, whether
it has known vulnerabilities and, for each, the advisory id, aliases (CVE), summary, severity, the
versions that fix it and a link. A coding or build agent asks this before it installs, pins or
ships a dependency, so the question repeats with every change.

It is a Web Read task (see service.Task): the same x402/MPP offers, validate -> deliver -> settle,
recovery of a paid answer without a second charge. **Charged only when the check is delivered**:
"no known vulnerabilities" is a delivered answer; OSV being unreachable costs nothing.

Data: OSV.dev's free public API (no key, no rate limit, as its docs state). Advisory data is
licensed per source; only language-package ecosystems are accepted, whose sources are CC-BY 4.0
(GitHub Advisory Database, PyPI, Go, OSS-Fuzz), CC0 (RustSec, Haskell) or MIT/Apache, and records
from non-commercial or share-alike feeds are dropped. Every answer credits OSV.dev and links each
advisory. Nothing here spends money. The package list is kept only with the paid result for the
recovery window; the sales ledger records counts and a hash, not package names.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, WithJsonSchema

OSV_API = "https://api.osv.dev/v1"
MAX_PACKAGES = 20
MAX_DETAILS = 60  # advisory records fetched per check; the rest are returned as ids and links
TIMEOUT_S = 10.0
CACHE_TTL_S = 6 * 3600
CACHE_MAX = 5000
# Language-package ecosystems (OSV names). Their advisory sources permit commercial reuse with
# attribution; OS-distribution feeds are left out (some are non-commercial or share-alike).
ECOSYSTEMS = ("npm", "PyPI", "Go", "crates.io", "Maven", "NuGet", "RubyGems", "Packagist", "Hex",
              "Pub", "Hackage", "CRAN", "SwiftURL")  # fmt: skip
# Record id prefixes from feeds whose licences don't allow this use (CC-BY-NC-ND, CC-BY-SA).
EXCLUDED_PREFIXES = ("CGA-", "UBUNTU-", "USN-")
# Attribution per advisory (CC-BY 4.0 asks for the source and licence): OSV id prefix ->
# (database, licence). Unknown prefixes are attributed to OSV.dev's aggregated record.
DATABASES = {
    "GHSA-": ("GitHub Advisory Database", "CC-BY-4.0"),
    "PYSEC-": ("PyPI Advisory Database", "CC-BY-4.0"),
    "GO-": ("Go Vulnerability Database", "CC-BY-4.0"),
    "RUSTSEC-": ("RustSec Advisory Database", "CC0-1.0"),
    "OSV-": ("OSS-Fuzz", "CC-BY-4.0"),
    "HSEC-": ("Haskell Security Advisories", "CC0-1.0"),
    "MAL-": ("OpenSSF Malicious Packages", "Apache-2.0"),
    "PSF-": ("Python Software Foundation", "CC-BY-4.0"),
    "RSEC-": ("RConsortium Advisory Database", "Apache-2.0"),
}
NOTE = ("No known vulnerabilities means OSV.dev had no matching advisory at checked_at; it is not "
        "a guarantee that a package is secure. A result with complete: false lists only the "
        "advisories retrieved.")  # fmt: skip
MAX_PAGES_PER_PACKAGE = 5
SOURCE = (
    "OSV.dev (https://osv.dev); advisory data licensed per source (CC-BY 4.0, CC0, MIT, Apache-2.0)"
)

DESCRIPTION = (
    "Check package versions for known vulnerabilities before installing or shipping them: up to 20 "
    "npm, PyPI, Go, crates.io, Maven, NuGet, RubyGems or Packagist packages per call, each with "
    "the advisory ids, CVE aliases, severity, fixed versions and links, from OSV.dev. Pay only "
    "when the "
    "check is delivered; a retry never pays twice."
)
MPP_DESCRIPTION = (
    "Known vulnerabilities for up to 20 package versions (advisory ids, severity, fixed versions), "
    "from OSV.dev. Charged only when the check is delivered."
)
SUMMARY = "Check package versions for known vulnerabilities (CVE, severity, fixed versions)"
TAGS = ["dependency vulnerability check", "package security", "cve lookup", "sca",
        "npm audit", "pip audit"]  # fmt: skip

Ecosystem = Literal["npm", "PyPI", "Go", "crates.io", "Maven", "NuGet", "RubyGems", "Packagist",
                    "Hex", "Pub", "Hackage", "CRAN", "SwiftURL"]  # fmt: skip


class PackageRef(BaseModel):
    ecosystem: Ecosystem = Field(..., description="OSV ecosystem name, e.g. npm, PyPI, Go")
    name: str = Field(..., min_length=1, max_length=214, description="Package name")
    version: str = Field(..., min_length=1, max_length=128, description="Exact version")


# Inline item schema: discovery indexes (the CDP Bazaar's extension validator among them) don't
# resolve `$ref`s into a nested `$defs`, so the published request schema must be self-contained.
_PACKAGE_ITEM = {
    "type": "object",
    "properties": {
        "ecosystem": {"type": "string", "enum": list(ECOSYSTEMS),
                      "description": "OSV ecosystem name, e.g. npm, PyPI, Go"},
        "name": {"type": "string", "minLength": 1, "maxLength": 214, "description": "Package name"},
        "version": {"type": "string", "minLength": 1, "maxLength": 128,
                    "description": "Exact version"},
    },
    "required": ["ecosystem", "name", "version"],
}  # fmt: skip


class CheckRequest(BaseModel):
    packages: Annotated[
        list[PackageRef],
        WithJsonSchema({"type": "array", "items": _PACKAGE_ITEM, "minItems": 1,
                        "maxItems": MAX_PACKAGES,
                        "description": "Package versions to check (1-20)"}),
    ] = Field(..., min_length=1, max_length=MAX_PACKAGES,
                                       description="Package versions to check (1-20)")  # fmt: skip
    request_id: Annotated[str | None, WithJsonSchema({"type": "string", "maxLength": 128})] = Field(
        None,
        max_length=128,
        description="Your id, echoed back. Retrying with the same packages and request_id within "
        "the "
        "recovery window returns the paid check without a second charge.",
    )


VULN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "aliases": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": ["string", "null"]},
        "severity": {"type": ["string", "null"]},
        "fixed": {"type": "array", "items": {"type": "string"}},
        "url": {"type": "string"},
        "database": {"type": "string"},
        "license": {"type": "string"},
    },
    "required": ["id", "url"],
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ecosystem": {"type": "string"},
                    "name": {"type": "string"},
                    "version": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["vulnerable", "no_known_vulnerabilities", "incomplete"],
                    },
                    "vulnerable": {"type": "boolean"},
                    "complete": {"type": "boolean"},
                    "vuln_count": {"type": "integer"},
                    "vulns": {"type": "array", "items": VULN_SCHEMA},
                },
                "required": ["ecosystem", "name", "version", "status", "vulnerable", "complete",
                             "vuln_count", "vulns"],
            },
        },
        "checked": {"type": "integer"},
        "vulnerable_packages": {"type": "integer"},
        "details_truncated": {"type": "boolean"},
        "source": {"type": "string"},
        "note": {"type": "string"},
        "checked_at": {"type": "string"},
        "results_sha256": {"type": "string"},
        "receipt": {"type": "object"},
    },
    "required": ["results", "checked", "vulnerable_packages", "source", "results_sha256"],
}


class _Cache:
    """Advisory records by id, kept CACHE_TTL_S (records change rarely; ids are stable)."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, dict[str, Any]]] = {}

    def get(self, key: str) -> dict[str, Any] | None:
        hit = self._rows.get(key)
        if hit is None or time.time() - hit[0] > CACHE_TTL_S:
            return None
        return hit[1]

    def put(self, key: str, value: dict[str, Any]) -> None:
        if len(self._rows) >= CACHE_MAX:
            for k in sorted(self._rows, key=lambda k: self._rows[k][0])[: CACHE_MAX // 10]:
                del self._rows[k]
        self._rows[key] = (time.time(), value)


def _severity(record: dict[str, Any]) -> str | None:
    db = record.get("database_specific") or {}
    if isinstance(db.get("severity"), str):
        return db["severity"]
    for s in record.get("severity") or []:
        if isinstance(s, dict) and s.get("score"):
            return str(s["score"])
    return None


def _fixed(record: dict[str, Any], ref: PackageRef) -> list[str]:
    out: list[str] = []
    for aff in record.get("affected") or []:
        pkg = aff.get("package") or {}
        if pkg.get("ecosystem") != ref.ecosystem or pkg.get("name") != ref.name:
            continue
        for rng in aff.get("ranges") or []:
            for ev in rng.get("events") or []:
                if (
                    isinstance(ev, dict)
                    and isinstance(ev.get("fixed"), str)
                    and ev["fixed"] not in out
                ):
                    out.append(ev["fixed"])
    return out


def _vuln(vid: str, record: dict[str, Any] | None, ref: PackageRef) -> dict[str, Any]:
    db, licence = next(((d, lic) for pre, (d, lic) in DATABASES.items() if vid.startswith(pre)),
                       ("OSV.dev", "see https://google.github.io/osv.dev/data/"))  # fmt: skip
    row: dict[str, Any] = {"id": vid, "url": f"https://osv.dev/vulnerability/{vid}",
                           "database": db, "license": licence}  # fmt: skip
    if record is not None:
        row.update(aliases=[a for a in record.get("aliases") or [] if isinstance(a, str)],
                   summary=record.get("summary"), severity=_severity(record),
                   fixed=_fixed(record, ref))  # fmt: skip
    else:
        row.update(aliases=[], summary=None, severity=None, fixed=[])
    return row


def _sha256(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


class OsvFailed(Exception):
    """OSV.dev couldn't answer the batch query; nothing is charged."""


async def check(body: CheckRequest, client: Any, cache: _Cache) -> dict[str, Any]:
    """The delivered check for `body`. Raises OsvFailed when OSV.dev's batch query fails."""
    queries = [{"package": {"ecosystem": p.ecosystem, "name": p.name}, "version": p.version}
               for p in body.packages]  # fmt: skip
    try:
        r = await client.post(f"{OSV_API}/querybatch", json={"queries": queries}, timeout=TIMEOUT_S)
        r.raise_for_status()
        batch = r.json().get("results") or []
    except Exception as exc:  # noqa: BLE001 - any upstream failure is an uncharged refusal
        raise OsvFailed(type(exc).__name__) from exc
    if len(batch) != len(queries):
        raise OsvFailed("result_count_mismatch")
    def keep(vulns: Any) -> list[str]:
        return [v["id"] for v in (vulns or []) if isinstance(v, dict) and "id" in v
                and not str(v["id"]).startswith(EXCLUDED_PREFIXES)]  # fmt: skip

    ids_per = [keep(row.get("vulns")) for row in batch]
    # OSV pages long result lists: a row with next_page_token has more advisories. Fetch them, so a
    # package is never reported short; if a page can't be fetched the package is marked incomplete.
    complete = [not row.get("next_page_token") for row in batch]
    for i, (row, q) in enumerate(zip(batch, queries, strict=True)):
        token = row.get("next_page_token")
        for _ in range(MAX_PAGES_PER_PACKAGE):
            if not token:
                complete[i] = True
                break
            try:
                more = await client.post(f"{OSV_API}/query", json={**q, "page_token": token},
                                         timeout=TIMEOUT_S)  # fmt: skip
                more.raise_for_status()
                page = more.json()
            except Exception:  # noqa: BLE001 - leave it marked incomplete, never clean
                break
            ids_per[i] += [v for v in keep(page.get("vulns")) if v not in ids_per[i]]
            token = page.get("next_page_token")
    wanted = list(dict.fromkeys(i for ids in ids_per for i in ids))
    fetch = [i for i in wanted[:MAX_DETAILS] if cache.get(i) is None]
    sem = asyncio.Semaphore(8)

    async def detail(vid: str) -> None:
        async with sem:
            try:
                d = await client.get(f"{OSV_API}/vulns/{vid}", timeout=TIMEOUT_S)
                if d.status_code == 200:
                    cache.put(vid, d.json())
            except Exception:  # noqa: BLE001 - a missing detail degrades to id + link
                pass

    await asyncio.gather(*(detail(v) for v in fetch))
    results = []
    for ref, ids, done in zip(body.packages, ids_per, complete, strict=True):
        vulns = [_vuln(v, cache.get(v) if v in wanted[:MAX_DETAILS] else None, ref) for v in ids]
        # Never "clean" unless OSV returned the whole answer for this package.
        status = "vulnerable" if vulns else ("no_known_vulnerabilities" if done else "incomplete")
        results.append({"ecosystem": ref.ecosystem, "name": ref.name, "version": ref.version,
                        "status": status, "vulnerable": bool(vulns), "complete": done,
                        "vuln_count": len(vulns), "vulns": vulns})  # fmt: skip
    return {
        "results": results,
        "checked": len(results),
        "vulnerable_packages": sum(1 for r in results if r["vulnerable"]),
        "details_truncated": len(wanted) > MAX_DETAILS,
        "note": NOTE,
        "source": SOURCE,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results_sha256": _sha256(results),
    }


def package_task(service: Any, secret: bytes, client: Any = None) -> Any:
    """The Package Check task for `service.create_app` (`service` is the loaded Web Read module).
    `client` is an httpx.AsyncClient (injected in tests); `secret` keys the retry identity."""
    if len(secret) < 16:
        raise ValueError("package_check_retry_secret_too_short")
    cache = _Cache()
    holder: dict[str, Any] = {"client": client}

    def http() -> Any:
        if holder["client"] is None:
            import httpx

            holder["client"] = httpx.AsyncClient(
                headers={"user-agent": "InferrailPackageCheck/1.0"}
            )
        return holder["client"]

    def idem(body: CheckRequest) -> str:
        message = json.dumps([[p.ecosystem, p.name, p.version] for p in body.packages]
                             + [body.request_id or ""]).encode()  # fmt: skip
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    async def deliver(
        body: CheckRequest, event: Callable[[str, str], None]
    ) -> tuple[int, dict[str, Any]]:
        try:
            row = await check(body, http(), cache)
        except OsvFailed as exc:
            event("refused", "upstream_failed")
            return 502, {"error": "upstream_failed", "detail": exc.args[0] if exc.args else "",
                         "charged": False}  # fmt: skip
        return 200, row

    def example(price_usd: str) -> dict[str, Any]:
        results = [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15",
                    "status": "vulnerable", "vulnerable": True, "complete": True,
                    "vuln_count": 1, "vulns": [{"id": "GHSA-35jh-r3h4-6jhm",
                    "aliases": ["CVE-2021-23337"], "summary": "Command Injection in lodash",
                    "severity": "HIGH", "fixed": ["4.17.21"],
                    "url": "https://osv.dev/vulnerability/GHSA-35jh-r3h4-6jhm",
                    "database": "GitHub Advisory Database", "license": "CC-BY-4.0"}]}]  # fmt: skip
        return {"results": results, "checked": 1, "vulnerable_packages": 1,
                "details_truncated": False, "note": NOTE, "source": SOURCE,
                "checked_at": "2026-11-22T00:00:00Z",
                "results_sha256": _sha256(results),
                "receipt": {"payment_ref": "0xpayer:0xnonce", "price_usd": price_usd}}  # fmt: skip

    def guide(settings: Any) -> str:
        return (
            "# Inferrail Package Check\n\n"
            f"> {DESCRIPTION}\n\n"
            f'POST {settings.resource_url} with JSON {{"packages": [{{"ecosystem": "npm", '
            '"name": "lodash", "version": "4.17.15"}], "request_id": "..."} (1-20 packages; '
            f"ecosystems: {', '.join(ECOSYSTEMS)}).\n"
            f"Price: ${settings.price_usd} per delivered check. The first response is HTTP 402.\n"
            + (
                f"Pay with x402 v2 exact, ${settings.price_usd} USDC on {settings.network} to "
                f"{settings.x402_recipient}: sign the 402 and repeat with PAYMENT-SIGNATURE.\n"
                if settings.x402
                else ""
            )
            + (
                f"Pay with MPP on Tempo (chain {settings.mpp.chain_id}): repeat the request with "
                "Authorization: Payment <credential>.\n"
                if settings.mpp
                else ""
            )
            + "HTTP 200 = check delivered and paid. Each result has status vulnerable, "
            "no_known_vulnerabilities or incomplete (complete: false lists only what OSV returned; "
            "never reported clean). No known vulnerabilities is not a guarantee of security. "
            "502 {error: upstream_failed} and 422 (invalid request) are not charged.\n"
            f"Data: {SOURCE}. Each advisory carries its OSV link; keep it when you show results.\n"
            f"Lost the response? Send the same packages and request_id again within "
            f"{settings.recovery_s // 60} minutes from the same wallet: no second charge.\n"
            "Your package list is kept only with the paid result for that recovery window; the "
            "sales record holds counts and a hash, not package names.\n"
        )

    return service.Task(
        path="/v1/check",
        request_model=CheckRequest,
        service_name="Inferrail Package Check",
        description=DESCRIPTION,
        mpp_description=MPP_DESCRIPTION,
        tags=["package-check", "vulnerability", "dependency", "osv"],
        input_example={"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}]},
        output_example=example,
        output_schema=OUTPUT_SCHEMA,
        idem=idem,
        deliver=deliver,
        ledger_fields=lambda row: (
            None,
            None,
            len(json.dumps(row["results"]).encode()),
            row["vulnerable_packages"],
            row["results_sha256"],
        ),  # fmt: skip
        guide=guide,
    )
