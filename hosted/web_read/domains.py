"""Inferrail Domain Check: DNS records, email authentication and registration for one domain.

`POST /v1/lookup {"domain": "example.com"}` returns the domain's DNS records (A, AAAA, CNAME, MX,
NS, TXT, SOA, CAA), its SPF and DMARC policies, and, where the registry's terms allow it, its
registration (registrar, status, created/expires dates, nameservers, DNSSEC) from the registry's
RDAP service. An agent asks this before it trusts a link, a sender or a counterparty, configures
email or DNS, or checks whether a name is taken, so the question repeats.

It is a Web Read task (see service.Task): the same x402/MPP offers, validate -> deliver -> settle,
recovery of a paid answer without a second charge. **Charged only when the lookup is delivered**:
"no such domain" is a delivered answer; the resolver being unreachable costs nothing.

Data and terms:
- DNS answers come from Cloudflare's public DNS-over-HTTPS resolver (1.1.1.1), which states no
  restriction on commercial or automated use. Google Public DNS is not used: its API terms forbid
  passing its results on to third parties.
- Registration comes live, one domain per paid request, only from registries whose RDAP terms
  were read and permit it (lawful, query-based use; no bulk or high-volume harvesting; no mass
  marketing): see RDAP_HOSTS. Other registries (Google Registry's pilot, CentralNic's "not to
  store or reproduce") are not queried and the answer says `registration_status: not_covered`.
  The registry's terms-of-service link is passed through with each record. Registrant contacts
  are never returned. Registrar referrals are not followed.
- The IANA RDAP bootstrap (public) maps a TLD to its registry.
Nothing here spends money. The domain is kept only with the paid result for the recovery window;
the sales ledger records counts and a hash, not the domain.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, WithJsonSchema, field_validator

DOH_URL = "https://cloudflare-dns.com/dns-query"
BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
DOH_TIMEOUT_S = 5.0
RDAP_TIMEOUT_S = 8.0
BOOTSTRAP_TTL_S = 24 * 3600
DNS_TTL_CAP_S = 300  # cache a DNS answer for min(its TTL, this)
RDAP_TTL_S = 900  # one registry query per domain per 15 minutes at most
CACHE_MAX = 5000
RDAP_CONCURRENCY = 2  # per registry host; keeps us far from any "high volume" pattern
RECORD_TYPES = ("A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA")
# DNS RR type numbers in Cloudflare's JSON answers.
TYPE_NUMBERS = {"A": 1, "NS": 2, "CNAME": 5, "SOA": 6, "MX": 15, "TXT": 16, "AAAA": 28,
                "CAA": 257}  # fmt: skip
# Registry RDAP hosts whose published terms permit live, per-request lookups relayed to the
# requester (reviewed 2026-11-22). Add a host only after reading its terms.
RDAP_HOSTS = {
    "rdap.verisign.com": "Verisign",  # .com, .net
    "rdap.publicinterestregistry.org": "Public Interest Registry",  # .org and others
    "rdap.identitydigital.services": "Identity Digital",  # .info, .ai and ~450 more
}
# Names that never have public DNS or registration: refused before payment matters.
RESERVED_SUFFIXES = ("localhost", "local", "internal", "intranet", "lan", "home", "corp",
                     "home.arpa", "onion", "invalid", "test", "alt")  # fmt: skip
_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_TLD = re.compile(r"^(?:[a-z]{2,63}|xn--[a-z0-9-]{1,59})$")

NOTE = (
    "DNS answers are what Cloudflare's resolver returned at checked_at. Registration comes from "
    "the registry's RDAP service when registration_status is ok; not_covered means the registry "
    "was not queried, not that the domain is unregistered. not_registered means the registry had "
    "no record at checked_at, not a guarantee that the name can be registered."
)
SOURCE = ("DNS: Cloudflare 1.1.1.1 DNS-over-HTTPS. Registration: the registry's RDAP service (see "
          "registration.terms_url), located through the IANA RDAP bootstrap.")  # fmt: skip

DESCRIPTION = (
    "Look up a domain: DNS records (A, AAAA, CNAME, MX, NS, TXT, SOA, CAA), SPF and DMARC email "
    "policies, and registration (registrar, status, created and expiry dates, nameservers, "
    "DNSSEC) from the registry's RDAP service for .com, .net, .org, .ai, .info and about 450 "
    "more TLDs. Pay only when the lookup is delivered; a retry never pays twice."
)
MPP_DESCRIPTION = (
    "DNS records, SPF/DMARC and RDAP registration (registrar, dates, status) for one domain. "
    "Charged only when the lookup is delivered."
)
SUMMARY = "Look up a domain's DNS records, SPF/DMARC and registration (RDAP)"
TAGS = ["dns lookup", "whois", "rdap", "domain registration", "mx records", "spf dmarc",
        "domain age"]  # fmt: skip

RecordType = Literal["A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA"]


def normalize_domain(raw: str) -> str:
    """The ASCII (IDNA) form of a registrable-looking domain name, or ValueError."""
    name = raw.strip().lower().removesuffix(".")
    if "://" in name or "/" in name or "@" in name or ":" in name or " " in name:
        raise ValueError("give a bare domain name such as example.com (no scheme, path or port)")
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("not a valid internationalized domain name") from exc
    labels = name.split(".")
    if len(labels) < 2 or len(name) > 253:
        raise ValueError("need a full domain name such as example.com")
    if not all(_LABEL.match(label) for label in labels) or not _TLD.match(labels[-1]):
        raise ValueError("not a valid domain name")
    if any(name == s or name.endswith("." + s) for s in RESERVED_SUFFIXES):
        raise ValueError("reserved or private name: it has no public DNS or registration")
    return name


class LookupRequest(BaseModel):
    domain: str = Field(..., min_length=3, max_length=253,
                        description="A domain name, e.g. example.com (IDN allowed)")  # fmt: skip
    records: Annotated[
        list[RecordType] | None,
        WithJsonSchema({"type": "array", "items": {"type": "string", "enum": list(RECORD_TYPES)},
                        "minItems": 1, "maxItems": len(RECORD_TYPES),
                        "description": "Record types to return (default: all)"}),
    ] = Field(None, min_length=1, max_length=len(RECORD_TYPES))  # fmt: skip
    registration: bool = Field(True, description="Include RDAP registration (default true)")
    request_id: Annotated[str | None, WithJsonSchema({"type": "string", "maxLength": 128})] = Field(
        None,
        max_length=128,
        description="Your id, echoed back. Retrying with the same domain and request_id within "
        "the recovery window returns the paid lookup without a second charge.",
    )

    @field_validator("domain")
    @classmethod
    def _domain(cls, v: str) -> str:
        return normalize_domain(v)

    def types(self) -> list[str]:
        return list(dict.fromkeys(self.records)) if self.records else list(RECORD_TYPES)


_RRSET = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["ok", "no_records", "nxdomain", "error"]},
        "values": {"type": "array", "items": {"type": "string"}},
        "ttl": {"type": ["integer", "null"]},
    },
    "required": ["status", "values"],
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "domain": {"type": "string"},
        "resolves": {"type": "boolean"},
        "dnssec_validated": {"type": ["boolean", "null"]},
        "dns": {"type": "object", "additionalProperties": _RRSET},
        "email": {
            "type": "object",
            "properties": {
                "spf": {"type": ["string", "null"]},
                "dmarc": {"type": ["string", "null"]},
                "dmarc_policy": {"type": ["string", "null"]},
                "mx_count": {"type": "integer"},
            },
        },
        "registration_status": {
            "type": "string",
            "enum": ["ok", "not_registered", "not_covered", "skipped", "error"],
        },
        "registration": {
            "type": ["object", "null"],
            "properties": {
                "registry": {"type": "string"},
                "registrar": {"type": ["string", "null"]},
                "registrar_iana_id": {"type": ["string", "null"]},
                "status": {"type": "array", "items": {"type": "string"}},
                "created": {"type": ["string", "null"]},
                "updated": {"type": ["string", "null"]},
                "expires": {"type": ["string", "null"]},
                "age_days": {"type": ["integer", "null"]},
                "nameservers": {"type": "array", "items": {"type": "string"}},
                "dnssec": {"type": ["boolean", "null"]},
                "rdap_url": {"type": "string"},
                "terms_url": {"type": ["string", "null"]},
            },
        },
        "complete": {"type": "boolean"},
        "source": {"type": "string"},
        "note": {"type": "string"},
        "checked_at": {"type": "string"},
        "results_sha256": {"type": "string"},
        "receipt": {"type": "object"},
    },
    "required": ["domain", "resolves", "dns", "email", "registration_status", "complete",
                 "source", "results_sha256"],
}  # fmt: skip


class _Cache:
    """Values by key, each with its own expiry, bounded in size."""

    def __init__(self) -> None:
        self._rows: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any:
        hit = self._rows.get(key)
        if hit is None or time.time() > hit[0]:
            return None
        return hit[1]

    def put(self, key: str, value: Any, ttl_s: float) -> None:
        if ttl_s <= 0:
            return
        if len(self._rows) >= CACHE_MAX:
            for k in sorted(self._rows, key=lambda k: self._rows[k][0])[: CACHE_MAX // 10]:
                del self._rows[k]
        self._rows[key] = (time.time() + ttl_s, value)


class ResolverFailed(Exception):
    """The DNS resolver couldn't answer; nothing is charged."""


def _sha256(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _txt(data: str) -> str:
    """A TXT answer's text: Cloudflare quotes each character-string; join them."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', data)
    return "".join(parts) if parts else data


async def _dns(client: Any, cache: _Cache, name: str, rtype: str) -> dict[str, Any]:
    key = f"dns|{name}|{rtype}"
    hit = cache.get(key)
    if hit is not None:
        return dict(hit)
    try:
        r = await client.get(DOH_URL, params={"name": name, "type": rtype},
                             headers={"accept": "application/dns-json"},
                             timeout=DOH_TIMEOUT_S)  # fmt: skip
        r.raise_for_status()
        body = r.json()
    except Exception:  # noqa: BLE001 - one failed type is reported, never shown as empty
        return {"status": "error", "values": [], "ttl": None, "ad": None}
    code = body.get("Status")
    if code == 3:
        row = {"status": "nxdomain", "values": [], "ttl": None, "ad": body.get("AD")}
    elif code != 0:
        return {"status": "error", "values": [], "ttl": None, "ad": None}
    else:
        want = TYPE_NUMBERS[rtype]
        answers = [a for a in body.get("Answer") or [] if isinstance(a, dict)
                   and a.get("type") == want and isinstance(a.get("data"), str)]  # fmt: skip
        values = [_txt(a["data"]) if rtype == "TXT" else a["data"] for a in answers]
        ttl = min((int(a.get("TTL") or 0) for a in answers), default=None)
        row = {"status": "ok" if values else "no_records", "values": values, "ttl": ttl,
               "ad": body.get("AD")}  # fmt: skip
    cache.put(key, row, min(row["ttl"] or 60, DNS_TTL_CAP_S))
    return dict(row)


class _Rdap:
    """IANA bootstrap + per-registry lookups, limited to RDAP_HOSTS."""

    def __init__(self) -> None:
        self.bootstrap: dict[str, list[str]] = {}
        self.bootstrap_at = 0.0
        self.sems: dict[str, asyncio.Semaphore] = {}

    async def base_url(self, client: Any, tld: str) -> str | None:
        if time.time() - self.bootstrap_at > BOOTSTRAP_TTL_S:
            try:
                r = await client.get(BOOTSTRAP_URL, timeout=RDAP_TIMEOUT_S)
                r.raise_for_status()
                mapping: dict[str, list[str]] = {}
                for tlds, urls in r.json().get("services") or []:
                    https = [u for u in urls if isinstance(u, str) and u.startswith("https://")]
                    for t in tlds:
                        mapping[str(t).lower()] = https
                self.bootstrap, self.bootstrap_at = mapping, time.time()
            except Exception:  # noqa: BLE001 - keep the last good bootstrap, if any
                if not self.bootstrap:
                    raise
        urls = self.bootstrap.get(tld) or []
        return urls[0] if urls else None


def _host(url: str) -> str:
    return url.split("/")[2].lower() if url.count("/") >= 2 else ""


def _date(events: list[Any], action: str) -> str | None:
    for ev in events:
        if isinstance(ev, dict) and ev.get("eventAction") == action:
            d = ev.get("eventDate")
            return d if isinstance(d, str) else None
    return None


def _registrar(entities: list[Any]) -> tuple[str | None, str | None]:
    for ent in entities:
        if not isinstance(ent, dict) or "registrar" not in (ent.get("roles") or []):
            continue
        name = None
        vcard = ent.get("vcardArray")
        if isinstance(vcard, list) and len(vcard) == 2 and isinstance(vcard[1], list):
            for prop in vcard[1]:
                if isinstance(prop, list) and len(prop) >= 4 and prop[0] == "fn":
                    name = str(prop[3]) or None
        iana = next((str(p.get("identifier")) for p in ent.get("publicIds") or []
                     if isinstance(p, dict) and p.get("type") == "IANA Registrar ID"),
                    None)  # fmt: skip
        return name, iana
    return None, None


def _terms(notices: list[Any]) -> str | None:
    for n in notices:
        if not isinstance(n, dict) or "term" not in str(n.get("title", "")).lower():
            continue
        for link in n.get("links") or []:
            if isinstance(link, dict) and isinstance(link.get("href"), str):
                return str(link["href"])
    return None


def parse_rdap(body: dict[str, Any], registry: str, url: str) -> dict[str, Any]:
    """The registration fields an agent needs; registrant contacts are never included."""
    events = body.get("events") or []
    created = _date(events, "registration")
    age = None
    if created:
        try:
            ts = time.mktime(time.strptime(created[:10], "%Y-%m-%d")) - time.timezone
            age = max(0, int((time.time() - ts) // 86400))
        except ValueError:
            age = None
    registrar, iana = _registrar(body.get("entities") or [])
    secure = body.get("secureDNS") or {}
    return {
        "registry": registry,
        "registrar": registrar,
        "registrar_iana_id": iana,
        "status": [s for s in body.get("status") or [] if isinstance(s, str)],
        "created": created,
        "updated": _date(events, "last changed"),
        "expires": _date(events, "expiration"),
        "age_days": age,
        "nameservers": sorted(
            str(ns.get("ldhName")).lower().removesuffix(".")
            for ns in body.get("nameservers") or []
            if isinstance(ns, dict) and ns.get("ldhName")
        ),
        "dnssec": secure.get("delegationSigned") if isinstance(secure, dict) else None,
        "rdap_url": url,
        "terms_url": _terms(body.get("notices") or []),
    }


async def _registration(client: Any, cache: _Cache, rdap: _Rdap, domain: str
                        ) -> tuple[str, dict[str, Any] | None]:  # fmt: skip
    tld = domain.rsplit(".", 1)[-1]
    try:
        base = await rdap.base_url(client, tld)
    except Exception:  # noqa: BLE001 - bootstrap unreachable: registration unknown
        return "error", None
    if base is None or _host(base) not in RDAP_HOSTS:
        return "not_covered", None
    # Query the registrable name the registry holds: the label just under the TLD. Multi-label
    # public suffixes (co.uk) belong to registries not in RDAP_HOSTS, so one label is enough.
    apex = ".".join(domain.split(".")[-2:])
    key = f"rdap|{apex}"
    hit = cache.get(key)
    if hit is not None:
        status, record = hit
        return str(status), record
    url = base.rstrip("/") + "/domain/" + apex
    host = _host(base)
    sem = rdap.sems.setdefault(host, asyncio.Semaphore(RDAP_CONCURRENCY))
    try:
        async with sem:
            r = await client.get(url, headers={"accept": "application/rdap+json"},
                                 timeout=RDAP_TIMEOUT_S, follow_redirects=False)  # fmt: skip
    except Exception:  # noqa: BLE001
        return "error", None
    if r.status_code == 404:
        out: tuple[str, dict[str, Any] | None] = ("not_registered", None)
    elif r.status_code == 200:
        try:
            out = ("ok", parse_rdap(r.json(), RDAP_HOSTS[host], url))
        except Exception:  # noqa: BLE001 - an unparseable record is unknown, not absent
            return "error", None
    else:
        return "error", None
    cache.put(key, out, RDAP_TTL_S)
    return out


async def lookup(body: LookupRequest, client: Any, cache: _Cache, rdap: _Rdap) -> dict[str, Any]:
    """The delivered lookup. Raises ResolverFailed when no DNS question could be answered."""
    domain = body.domain
    types = body.types()
    want_txt = "TXT" in types
    names = [(domain, t) for t in types] + [(f"_dmarc.{domain}", "TXT")]
    if not want_txt:
        names.append((domain, "TXT"))  # for SPF

    async def registration() -> tuple[str, dict[str, Any] | None]:
        if not body.registration:
            return "skipped", None
        return await _registration(client, cache, rdap, domain)

    answers, (reg_status, reg) = await asyncio.gather(
        asyncio.gather(*(_dns(client, cache, n, t) for n, t in names)), registration()
    )
    dns_rows: dict[str, dict[str, Any]] = dict(zip(types, answers[: len(types)], strict=True))
    dmarc_row = answers[len(types)]
    txt_row = dns_rows["TXT"] if want_txt else answers[len(types) + 1]
    if all(row["status"] == "error" for row in dns_rows.values()):
        raise ResolverFailed("dns_unreachable")
    ad = next((row["ad"] for row in dns_rows.values() if row.get("ad") is not None), None)
    for row in dns_rows.values():
        row.pop("ad", None)
    spf = next((v for v in txt_row["values"] if v.lower().startswith("v=spf1")), None)
    dmarc = next((v for v in dmarc_row["values"] if v.lower().startswith("v=dmarc1")), None)
    policy = None
    if dmarc:
        m = re.search(r"(?:^|;)\s*p\s*=\s*([a-z]+)", dmarc, re.I)
        policy = m.group(1).lower() if m else None
    nx = any(row["status"] == "nxdomain" for row in dns_rows.values())
    resolves = not nx and any(row["status"] == "ok" for row in dns_rows.values())
    complete = (all(row["status"] != "error" for row in dns_rows.values())
                and txt_row["status"] != "error" and dmarc_row["status"] != "error"
                and reg_status != "error")  # fmt: skip
    mx = dns_rows.get("MX")
    result = {
        "domain": domain,
        "resolves": resolves,
        "dnssec_validated": ad,
        "dns": dns_rows,
        "email": {
            "spf": spf,
            "dmarc": dmarc,
            "dmarc_policy": policy,
            "mx_count": len(mx["values"]) if mx else 0,
        },  # fmt: skip
        "registration_status": reg_status,
        "registration": reg,
        "complete": complete,
    }
    return {
        **result,
        "note": NOTE,
        "source": SOURCE,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "results_sha256": _sha256(result),
    }


def domain_task(service: Any, secret: bytes, client: Any = None) -> Any:
    """The Domain Check task for `service.create_app` (`service` is the loaded Web Read module).
    `client` is an httpx.AsyncClient (injected in tests); `secret` keys the retry identity."""
    if len(secret) < 16:
        raise ValueError("domain_check_retry_secret_too_short")
    cache = _Cache()
    rdap = _Rdap()
    holder: dict[str, Any] = {"client": client}

    def http() -> Any:
        if holder["client"] is None:
            import httpx

            holder["client"] = httpx.AsyncClient(headers={"user-agent": "InferrailDomainCheck/1.0"})
        return holder["client"]

    def idem(body: LookupRequest) -> str:
        message = json.dumps([body.domain, body.types(), body.registration,
                              body.request_id or ""]).encode()  # fmt: skip
        return hmac.new(secret, message, hashlib.sha256).hexdigest()

    async def deliver(
        body: LookupRequest, event: Callable[[str, str], None]
    ) -> tuple[int, dict[str, Any]]:
        try:
            row = await lookup(body, http(), cache, rdap)
        except ResolverFailed as exc:
            event("refused", "upstream_failed")
            return 502, {"error": "upstream_failed", "detail": exc.args[0] if exc.args else "",
                         "charged": False}  # fmt: skip
        return 200, row

    def example(price_usd: str) -> dict[str, Any]:
        dns = {"A": {"status": "ok", "values": ["93.184.215.14"], "ttl": 300},
               "MX": {"status": "ok", "values": ["0 ."], "ttl": 300},
               "TXT": {"status": "ok", "values": ["v=spf1 -all"], "ttl": 300}}  # fmt: skip
        result = {
            "domain": "example.com", "resolves": True, "dnssec_validated": True, "dns": dns,
            "email": {"spf": "v=spf1 -all", "dmarc": "v=DMARC1;p=reject;sp=reject;adkim=s;aspf=s",
                      "dmarc_policy": "reject", "mx_count": 1},
            "registration_status": "ok",
            "registration": {
                "registry": "Verisign", "registrar": "RESERVED-Internet Assigned Numbers Authority",
                "registrar_iana_id": "376",
                "status": ["client delete prohibited", "client transfer prohibited"],
                "created": "1995-08-14T04:00:00Z", "updated": "2026-01-16T18:26:50Z",
                "expires": "2027-08-13T04:00:00Z", "age_days": 11423,
                "nameservers": ["a.iana-servers.net", "b.iana-servers.net"], "dnssec": True,
                "rdap_url": "https://rdap.verisign.com/com/v1/domain/example.com",
                "terms_url": "https://www.verisign.com/domain-names/registration-data-access-"
                             "protocol/terms-service/index.xhtml",
            },
            "complete": True,
        }  # fmt: skip
        return {**result, "note": NOTE, "source": SOURCE, "checked_at": "2026-11-22T00:00:00Z",
                "results_sha256": _sha256(result),
                "receipt": {"payment_ref": "0xpayer:0xnonce", "price_usd": price_usd}}  # fmt: skip

    def guide(settings: Any) -> str:
        return (
            "# Inferrail Domain Check\n\n"
            f"> {DESCRIPTION}\n\n"
            f'POST {settings.resource_url} with JSON {{"domain": "example.com", '
            '"request_id": "..."}. Optional: "records": a subset of '
            f'{", ".join(RECORD_TYPES)} (default all); "registration": false to skip RDAP.\n'
            f"Price: ${settings.price_usd} per delivered lookup. The first response is HTTP 402.\n"
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
            + "HTTP 200 = lookup delivered and paid, including a domain that does not exist "
            "(resolves: false, dns statuses nxdomain). Each record type has status ok, "
            "no_records, nxdomain or error; complete: false means some part could not be "
            "answered and is marked error, never shown as empty. registration_status is ok, "
            "not_registered (the registry had no record), not_covered (that registry is not "
            "queried; not a sign the name is free), skipped or error. Registrant contacts are "
            "never returned. 502 {error: upstream_failed} and 422 (invalid or reserved name) "
            "are not charged.\n"
            f"Data: {SOURCE} Registry data is for lawful use only; do not use it for bulk "
            "harvesting or unsolicited marketing.\n"
            f"Lost the response? Send the same request and request_id again within "
            f"{settings.recovery_s // 60} minutes from the same wallet: no second charge.\n"
            "The domain is kept only with the paid result for that recovery window; the sales "
            "record holds counts and a hash, not the domain.\n"
        )

    return service.Task(
        result_noun="domain lookup",
        credits_ref="domain-check",
        path="/v1/lookup",
        request_model=LookupRequest,
        service_name="Inferrail Domain Check",
        description=DESCRIPTION,
        mpp_description=MPP_DESCRIPTION,
        tags=["domain-check", "dns", "rdap", "whois"],
        input_example={"domain": "example.com"},
        output_example=example,
        output_schema=OUTPUT_SCHEMA,
        idem=idem,
        deliver=deliver,
        ledger_fields=lambda row: (
            None,
            None,
            len(json.dumps(row["dns"]).encode()),
            sum(len(r["values"]) for r in row["dns"].values()),
            row["results_sha256"],
        ),  # fmt: skip
        guide=guide,
    )
