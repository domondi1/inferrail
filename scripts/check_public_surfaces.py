"""Validate the small public product inventory and static adoption journeys.

Offline by default. --live performs only unsigned, unauthenticated contract probes;
it never creates a workspace, initiates checkout or authorizes payment.
"""

from __future__ import annotations

import argparse
import base64
import json
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import unquote, urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
PAGES = (
    "index.html", "web-read/index.html", "checks/index.html", "pricing/index.html",
    "developers/index.html", "tools/index.html", "hosted/index.html",
    "spending-controls/index.html", "try/index.html", "labs/index.html", "ap/index.html",
)


class Page(HTMLParser):
    def __init__(self, text: str) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: list[str] = []
        self.links: list[str] = []
        self.h1s = 0
        self.canonical = ""
        self.missing_alt = 0
        self.feed(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if values.get("id"):
            self.ids.append(str(values["id"]))
        if tag == "h1":
            self.h1s += 1
        if tag == "img" and "alt" not in values:
            self.missing_alt += 1
        if tag == "link" and values.get("rel") == "canonical":
            self.canonical = str(values.get("href", ""))
        for attr in ("href", "src"):
            if values.get(attr):
                self.links.append(str(values[attr]))


def resolve_link(page: Path, href: str) -> tuple[Path, str] | None:
    url = urlsplit(href)
    if url.scheme or url.netloc:
        return None
    path = unquote(url.path)
    target = (DOCS / path.lstrip("/")) if path.startswith("/") else page.parent / path
    if not path:
        target = page
    if target.is_dir():
        target /= "index.html"
    # GitHub Pages/Jekyll renders these retained Markdown document URLs as HTML.
    if not target.exists() and target.suffix == ".html" and target.with_suffix(".md").exists():
        target = target.with_suffix(".md")
    return target, unquote(url.fragment)


def check_local() -> list[str]:
    errors: list[str] = []
    for relative in PAGES:
        path = DOCS / relative
        text = path.read_text()
        parsed = Page(text)
        if parsed.h1s != 1 or parsed.missing_alt or len(parsed.ids) != len(set(parsed.ids)):
            errors.append(f"{relative}: check heading, image alt or duplicate IDs")
        if not parsed.canonical.startswith("https://tryinferrail.com/"):
            errors.append(f"{relative}: missing production canonical")
        for href in parsed.links:
            resolved = resolve_link(path, href)
            if resolved is None:
                continue
            target, fragment = resolved
            if not target.exists():
                errors.append(f"{relative}: broken local link {href}")
            elif fragment and target.suffix == ".html" and fragment not in Page(
                target.read_text()
            ).ids:
                errors.append(f"{relative}: missing anchor {href}")

    catalog = json.loads((DOCS / "product-catalog.json").read_text())
    website = (DOCS / "llms.txt").read_text()
    repo = (ROOT / "llms.txt").read_text()
    readme = (ROOT / "README.md").read_text()
    pricing = (DOCS / "pricing/index.html").read_text()
    for product in catalog["products"]:
        if not product.get("evidence") or not product.get("verified_at"):
            errors.append(f"{product['id']}: missing availability evidence/date")
        if product["availability"] != "live_commercial":
            continue
        if product["endpoint"] not in website or product["endpoint"] not in repo:
            errors.append(f"{product['id']}: missing machine discovery endpoint")
        if "$" + product["price_usd"] not in pricing:
            errors.append(f"{product['id']}: pricing page differs from inventory")
    if "read/credits/v1/page" not in readme or "inferrailCreditTools" not in readme:
        errors.append("README: missing no-wallet purchase path")
    for text in (website, repo, readme):
        for required in ("10 minutes", "5 MB", "50", "0.3.0", "get_spend", "get_health"):
            if required not in text:
                errors.append(f"Public guide missing contract detail: {required}")
    return errors


def probe(origin: str, path: str, body: dict[str, object] | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = Request(origin + path, data=data, headers={"Content-Type": "application/json"})
    try:
        response = urlopen(request, timeout=30)  # noqa: S310 - fixed public product origin
    except HTTPError as exc:
        response = exc
    with response:
        raw = response.read()
        result = json.loads(raw)
        offer = response.headers.get("payment-required")
        if offer:
            result["_offer"] = json.loads(base64.b64decode(offer))
        return response.status, result


def check_live() -> list[str]:
    catalog = json.loads((DOCS / "product-catalog.json").read_text())
    origin = catalog["production_origin"]
    inputs = {
        "web-read": {"url": "https://example.com/"},
        "package-check": {"packages": [{"ecosystem": "npm", "name": "lodash",
                                         "version": "4.17.20"}]},
        "domain-check": {"domain": "example.com"},
        "finder": {"need": "read a public web page"},
    }
    errors: list[str] = []
    for product in catalog["products"]:
        body = inputs.get(product["id"])
        if body is None:
            continue
        status, result = probe(origin, product["endpoint"], body)
        offers = result.get("_offer", {}).get("accepts", [])
        expected = int(Decimal(product["price_usd"]) * 1_000_000)
        if status != 402 or not any(
            x["network"] == "eip155:8453" and int(x["amount"]) == expected for x in offers
        ):
            errors.append(f"{product['name']}: live price/network/availability changed")
        print(f"{product['name']}: unsigned HTTP {status}")
        if "credits_endpoint" in product:
            status, result = probe(origin, product["credits_endpoint"], body)
            if status != 401 or result.get("credits_per_result") != product["credits_per_result"]:
                errors.append(f"{product['name']}: API-key activation/price changed")
            print(f"{product['name']} credits: unauthenticated HTTP {status}")
    status, terms = probe(origin, "/hosted/pricing")
    if status != 200 or terms.get("free_governed_runs_per_month") != 250:
        errors.append("Hosted allowance changed")
    if terms.get("credits", {}).get("card", {}).get("price_usd") != "10.00":
        errors.append("Card offer unavailable or changed")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="also probe unpaid public contracts")
    args = parser.parse_args()
    errors = check_local()
    if args.live:
        errors.extend(check_live())
    for error in errors:
        print("FAIL:", error)
    print(f"Public surfaces: {len(errors)} errors; {len(PAGES)} pages checked.")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
