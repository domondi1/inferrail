"""Model prices read from the vendors' own pricing pages, the only source this project treats
as authoritative (see `inferrail.pricing.builtin`'s docstring and docs/adr/0005).

OpenAI and Anthropic publish their pricing pages as markdown. `parse_openai` and
`parse_anthropic` read the standard-tier tables into `PriceEntry` objects, and
`scripts/sync_vendor_pricing.py` writes the result to the versioned snapshot
(`vendor_snapshot.json`) that `PricingResolver` reads. A price only reaches budget
enforcement through a reviewed change to that file.

Rows are refused, never approximated, when a price can't be represented exactly:

- a context tier whose threshold the page doesn't state;
- an Anthropic model the page doesn't say is billed at one rate across its whole context
  window (older models may carry a long-context premium);
- a long-context tier with cache-write rates (the price schema can't express it yet);
- a retired model, or a row whose numbers fail the sanity checks.

A refused model stays unpriced: a budgeted request for it is refused (INFERRAIL_E012), never
priced from a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from inferrail.config.models import LongContextPrice, PriceEntry

OPENAI_PRICING_PAGE = "https://developers.openai.com/api/docs/pricing.md"
ANTHROPIC_PRICING_PAGE = "https://platform.claude.com/docs/en/about-claude/pricing.md"


@dataclass(frozen=True)
class Parsed:
    prices: dict[str, PriceEntry]
    skipped: dict[str, str]  # model id -> why it stays unpriced


def _tables(md: str) -> list[tuple[list[str], list[list[str]]]]:
    """Markdown tables as (header, rows), in page order."""
    out: list[tuple[list[str], list[list[str]]]] = []
    block: list[list[str]] = []
    for line in [*md.splitlines(), ""]:
        if line.startswith("|"):
            block.append([c.strip() for c in line.strip().strip("|").split("|")])
            continue
        if len(block) >= 2 and set(block[1][0]) <= set("-: "):
            out.append((block[0], block[2:]))
        block = []
    return out


def _section(md: str, start_heading: str, *, level: str) -> str:
    """The text from `start_heading` up to the next heading of the same level ("" if absent)."""
    start = md.find(start_heading)
    if start < 0:
        return ""
    end = md.find(f"\n{level} ", start + len(start_heading))
    return md[start:] if end < 0 else md[start:end]


def _usd(cell: str) -> Decimal | None:
    """`$1.25`, `$1.25 / MTok`, `$0.20 / MTok<sup>2</sup>` -> Decimal; `-` or text -> None."""
    m = re.match(r"^\$([0-9]+(?:\.[0-9]+)?)", cell.strip())
    return Decimal(m.group(1)) if m else None


def _sane(entry: PriceEntry) -> bool:
    """Guards against a misread table: a cache read must cost less than input, and input may not
    exceed output by more than tenfold (no current model is priced that way)."""
    read = entry.cache_read_usd_per_million
    if read is not None and read >= entry.input_usd_per_million:
        return False
    return entry.input_usd_per_million <= entry.output_usd_per_million * 10


def parse_openai(md: str, verified: date) -> Parsed:
    """OpenAI's "Standard pricing data" section: a tiered table (short/long context, some with
    cache-write columns) and a flat table (input, output, cached input)."""
    prices: dict[str, PriceEntry] = {}
    skipped: dict[str, str] = {}
    section = _section(md, "### Standard pricing data", level="###")
    for header, rows in _tables(section):
        cols = [h.lower() for h in header]
        for row in rows:
            cells = dict(zip(cols, row, strict=False))
            name = row[0]
            threshold = re.search(r"\(<\s*(\d+)K context length\)", name)
            model = re.sub(r"\s*\(.*?\)", "", name).strip()
            if not re.fullmatch(r"[a-z0-9][a-z0-9.\-]*", model):
                continue
            if "short context input" in cells:
                writes = [
                    c for c in cols if "write" in c and _usd(cells.get(c, "")) is not None
                ]
                long_in = _usd(cells.get("long context input", ""))
                if long_in is not None and threshold is None:
                    skipped[model] = "long-context threshold not stated on the pricing page"
                    continue
                if long_in is not None and any(c.startswith("long") for c in writes):
                    skipped[model] = "long-context cache-write rates not representable yet"
                    continue
                base_in = _usd(cells.get("short context input", ""))
                base_out = _usd(cells.get("short context output", ""))
                base_read = _usd(cells.get("short context cached input", ""))
                long_out = _usd(cells.get("long context output", ""))
                long_read = _usd(cells.get("long context cached input", ""))
            else:
                base_in = _usd(cells.get("input", ""))
                base_out = _usd(cells.get("output", ""))
                base_read = _usd(cells.get("cached input", ""))
                long_in = long_out = long_read = None
            if base_in is None or base_out is None:
                skipped[model] = "input or output price missing"
                continue
            long_context = None
            if long_in is not None and threshold is not None:
                if long_out is None or (base_read is None) != (long_read is None):
                    skipped[model] = "incomplete long-context rates"
                    continue
                long_context = LongContextPrice(
                    above_input_tokens=int(threshold.group(1)) * 1000,
                    input_usd_per_million=long_in,
                    output_usd_per_million=long_out,
                    cache_read_usd_per_million=long_read,
                )
            entry = PriceEntry(
                input_usd_per_million=base_in,
                output_usd_per_million=base_out,
                cache_read_usd_per_million=base_read,
                long_context=long_context,
                source=OPENAI_PRICING_PAGE,
                verified_date=verified,
            )
            if not _sane(entry):
                skipped[model] = "failed sanity checks"
                continue
            prices.setdefault(model, entry)  # first table wins if a model appears twice
    return Parsed(prices, skipped)


_FLAT_RULE = re.compile(
    r"Claude (\d+)\.(\d+) and later models \(except ([^)]*)\)[^.]*include the full"
    r" \[?1M token context window"
)


def _anthropic_id(name: str) -> tuple[str, tuple[int, int]] | None:
    m = re.fullmatch(r"Claude ([A-Za-z]+) (\d+)(?:\.(\d+))?", name)
    if not m:
        return None
    major, minor = int(m.group(2)), int(m.group(3) or 0)
    model = f"claude-{m.group(1).lower()}-{major}" + (f"-{minor}" if m.group(3) else "")
    return model, (major, minor)


def parse_anthropic(md: str, verified: date) -> Parsed:
    """Anthropic's "Model pricing" table (base input, 5m and 1h cache writes, cache hits,
    output). Only models the page says are billed at one rate across their full context window
    are priced; the rule is read from the page itself, so nothing here pins a model version."""
    prices: dict[str, PriceEntry] = {}
    skipped: dict[str, str] = {}
    flat_text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", md)
    rule = _FLAT_RULE.search(flat_text)
    section = _section(md, "## Model pricing", level="##")
    for header, rows in _tables(section)[:1]:
        cols = [h.lower() for h in header]
        for row in rows:
            raw = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", row[0])
            note = re.search(r"\((.*)\)", raw)
            ident = _anthropic_id(re.sub(r"\s*\(.*\)", "", raw).strip())
            if ident is None:
                continue
            model, version = ident
            if note and "retired" in note.group(1).lower():
                skipped[model] = "retired"
                continue
            if note and "tokens" in note.group(1).lower():
                skipped[model] = (
                    "priced by prompt length with cache-write rates; not representable yet"
                )
                continue
            if rule is None:
                skipped[model] = "page no longer states which models have flat long-context pricing"
                continue
            excepted = {_anthropic_id(x.strip()) for x in rule.group(3).split(",")}
            if version < (int(rule.group(1)), int(rule.group(2))) or (model, version) in excepted:
                skipped[model] = "page doesn't state flat pricing across its full context window"
                continue
            cells = dict(zip(cols, row, strict=False))

            def rate(
                word: str, cells: dict[str, str] = cells, cols: list[str] = cols
            ) -> Decimal | None:
                col = next((c for c in cols if word in c), None)
                return _usd(cells[col]) if col else None

            base_in, out = rate("base input"), rate("output")
            w5, w1, hit = rate("5m cache"), rate("1h cache"), rate("cache hits")
            if None in (base_in, out, w5, w1, hit):
                skipped[model] = "a rate is missing"
                continue
            entry = PriceEntry(
                input_usd_per_million=base_in,  # type: ignore[arg-type]
                output_usd_per_million=out,  # type: ignore[arg-type]
                cache_write_5m_usd_per_million=w5,
                cache_write_1h_usd_per_million=w1,
                cache_read_usd_per_million=hit,
                source=ANTHROPIC_PRICING_PAGE,
                verified_date=verified,
            )
            if not _sane(entry):
                skipped[model] = "failed sanity checks"
                continue
            prices[model] = entry
    return Parsed(prices, skipped)


# -- the versioned snapshot ------------------------------------------------------------------

SNAPSHOT_FILE = "vendor_snapshot.json"


def snapshot_document(
    parsed: dict[str, Parsed], digests: dict[str, str], generated: date
) -> dict[str, object]:
    """The JSON written to `vendor_snapshot.json`: prices per provider type, why each refused
    model stays unpriced, and a digest of every page read, so a review can see exactly what
    changed and where it came from."""
    return {
        "schema": 1,
        "generated": generated.isoformat(),
        "sources": {
            "openai": {"url": OPENAI_PRICING_PAGE, "sha256": digests.get("openai")},
            "anthropic": {"url": ANTHROPIC_PRICING_PAGE, "sha256": digests.get("anthropic")},
        },
        "prices": {
            vendor: {m: e.model_dump(mode="json") for m, e in sorted(p.prices.items())}
            for vendor, p in parsed.items()
        },
        "unpriced": {vendor: dict(sorted(p.skipped.items())) for vendor, p in parsed.items()},
    }


def load_snapshot() -> dict[str, dict[str, PriceEntry]]:
    """Prices from the packaged snapshot, by provider type. A missing or unreadable snapshot
    yields nothing (the built-in catalog still applies); an entry that fails validation is
    dropped, never repaired."""
    import json
    from importlib import resources

    try:
        raw = json.loads(resources.files(__package__).joinpath(SNAPSHOT_FILE).read_text())
    except (OSError, ValueError):
        return {}
    out: dict[str, dict[str, PriceEntry]] = {}
    for vendor, models in (raw.get("prices") or {}).items():
        valid: dict[str, PriceEntry] = {}
        for model, data in models.items():
            try:
                valid[model] = PriceEntry.model_validate(data)
            except ValueError:
                continue
        out[vendor] = valid
    return out
