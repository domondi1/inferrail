"""Regenerate src/inferrail/pricing/vendor_snapshot.json from the vendors' own pricing pages.

    python3 scripts/sync_vendor_pricing.py           # report what would change
    python3 scripts/sync_vendor_pricing.py --write   # write the snapshot

Exit status: 0 unchanged, 1 changed (with --write: written), 2 a page couldn't be read or
parsed into anything (the snapshot is left as it is). The scheduled workflow turns a change
into a pull request; a price reaches budget enforcement only once that is reviewed and merged.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from inferrail.pricing import vendor_pages as vp  # noqa: E402

SNAPSHOT = ROOT / "src" / "inferrail" / "pricing" / vp.SNAPSHOT_FILE
PAGES = {"openai": vp.OPENAI_PRICING_PAGE, "anthropic": vp.ANTHROPIC_PRICING_PAGE}
PARSERS = {"openai": vp.parse_openai, "anthropic": vp.parse_anthropic}


def fetch(url: str) -> str:
    req = urllib.request.Request(url, headers={"user-agent": "inferrail-pricing-sync/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--pages", type=Path, help="read <vendor>.md from here instead of fetching")
    a = ap.parse_args()
    today = datetime.now(UTC).date()
    parsed, digests = {}, {}
    for vendor, url in PAGES.items():
        try:
            md = (a.pages / f"{vendor}.md").read_text() if a.pages else fetch(url)
        except Exception as e:
            print(f"error: {vendor} pricing page unavailable: {e!r}", file=sys.stderr)
            return 2
        result = PARSERS[vendor](md, today)
        if not result.prices:
            print(f"error: no prices parsed from {url}; page format changed?", file=sys.stderr)
            return 2
        parsed[vendor], digests[vendor] = result, hashlib.sha256(md.encode()).hexdigest()
    new = vp.snapshot_document(parsed, digests, today)
    old = json.loads(SNAPSHOT.read_text()) if SNAPSHOT.exists() else {}
    changed = False
    for vendor in PAGES:
        before = (old.get("prices") or {}).get(vendor, {})
        after = new["prices"][vendor]  # type: ignore[index]
        strip = lambda e: {k: v for k, v in e.items() if k != "verified_date"}  # noqa: E731
        for m in sorted(set(before) | set(after)):
            if m not in after:
                why = new["unpriced"][vendor].get(m, "not on page")  # type: ignore[index]
                print(f"{vendor}: {m} no longer priced ({why})")
            elif m not in before:
                print(f"{vendor}: {m} newly priced")
            elif strip(before[m]) != strip(after[m]):
                print(f"{vendor}: {m} price changed")
            else:
                continue
            changed = True
    if not changed:
        print("pricing snapshot unchanged")
        return 0
    if a.write:
        SNAPSHOT.write_text(json.dumps(new, indent=1) + "\n")
        print(f"wrote {SNAPSHOT.relative_to(ROOT)}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
