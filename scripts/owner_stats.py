"""Owner-side install/usage counting -- run directly, no arguments needed
for the zero-telemetry layer:

    python3 scripts/owner_stats.py
    python3 scripts/owner_stats.py --db /path/to/usage-ping.sqlite3

Two independent layers, matching
docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md:

**Layer 1 -- zero telemetry, always run.** Pulls PyPI download counts for
the `inferrail` package from pypistats.org's public, unauthenticated API
and prints daily/weekly/monthly totals. Costs installers nothing, needs
no consent, and works even if the usage-ping beacon (layer 2) is off.

**Layer 2 -- the usage-ping collector's own database, if `--db` is
given.** Runs the exact queries
docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md
specifies against `hosted/usage_ping/service.py`'s `installs`/`events`
tables: total installs, activation (reached a real receipt), active in
the last 7/30 days, new installs per week, and the activation rate.
Skipped entirely (with a clear note, not an error) if `--db` isn't
given -- this script never assumes a collector is deployed.

This script itself never sends anything anywhere -- it only *reads* a
public API and a local file the operator already controls.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

PYPI_PACKAGE = "inferrail"
PYPISTATS_URL = f"https://pypistats.org/api/packages/{PYPI_PACKAGE}/recent"
_REQUEST_TIMEOUT_SECONDS = 10.0


def fetch_pypi_download_counts() -> dict[str, int] | None:
    """`{"last_day": N, "last_week": N, "last_month": N}` from
    pypistats.org's public `recent` endpoint, or `None` if the request
    fails for any reason (offline, rate-limited, pypistats down) -- never
    raises, since this script's whole point is to be a quick, harmless
    status check, not another thing that can break a release process."""
    try:
        import json

        with urlopen(PYPISTATS_URL, timeout=_REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
            body = json.loads(response.read())
        data = body["data"]
        return {
            "last_day": data["last_day"],
            "last_week": data["last_week"],
            "last_month": data["last_month"],
        }
    except (URLError, TimeoutError, KeyError, ValueError) as exc:
        print(f"  (could not reach pypistats.org: {exc})", file=sys.stderr)
        return None


def print_pypi_stats() -> None:
    print("=" * 72)
    print(f"PyPI download counts -- {PYPI_PACKAGE} (pypistats.org, public, no auth)")
    print("=" * 72)
    counts = fetch_pypi_download_counts()
    if counts is None:
        print("  unavailable this run -- see stderr for why")
        return
    print(f"  last day:   {counts['last_day']:,}")
    print(f"  last week:  {counts['last_week']:,}")
    print(f"  last month: {counts['last_month']:,}")
    print()
    print(f"  Full history: https://pypistats.org/packages/{PYPI_PACKAGE}")


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def print_collector_stats(db_path: Path) -> None:
    print()
    print("=" * 72)
    print(f"Usage-ping collector stats -- {db_path}")
    print("=" * 72)
    if not db_path.exists():
        print(f"  no database found at {db_path} -- nothing to report")
        return

    conn = _connect(db_path)
    try:
        total_installs = conn.execute("SELECT COUNT(*) FROM installs").fetchone()[0]
        activated = conn.execute(
            "SELECT COUNT(*) FROM installs WHERE reached_first_receipt_at IS NOT NULL"
        ).fetchone()[0]
        now = datetime.now(UTC)
        active_7d = conn.execute(
            "SELECT COUNT(*) FROM installs WHERE last_seen_at > ?",
            ((now - timedelta(days=7)).isoformat(),),
        ).fetchone()[0]
        active_30d = conn.execute(
            "SELECT COUNT(*) FROM installs WHERE last_seen_at > ?",
            ((now - timedelta(days=30)).isoformat(),),
        ).fetchone()[0]
        first_seen_rows = conn.execute("SELECT first_seen_at FROM installs").fetchall()
    finally:
        conn.close()

    print(f"  Total installs:              {total_installs:,}")
    print(f"  Activated (reached a real receipt): {activated:,}")
    rate = f"{activated / total_installs:.1%}" if total_installs else "n/a (no installs yet)"
    print(f"  Activation rate:             {rate}")
    print(f"  Active in the last 7 days:   {active_7d:,}")
    print(f"  Active in the last 30 days:  {active_30d:,}")
    print()
    print("  New installs per week (ISO week of first_seen_at):")
    weekly = _weekly_counts(row["first_seen_at"] for row in first_seen_rows)
    if not weekly:
        print("    (no installs recorded yet)")
    for week, count in sorted(weekly.items()):
        print(f"    {week}: {count}")


def _weekly_counts(timestamps: object) -> Counter[str]:
    """Groups ISO timestamps by ISO year-week (`YYYY-Www`) -- the same
    grouping `date_trunc('week', first_seen_at)` gives in Postgres,
    computed in Python here since SQLite has no native week-truncation
    function."""
    counts: Counter[str] = Counter()
    for ts in timestamps:  # type: ignore[attr-defined]
        try:
            dt = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        year, week, _ = dt.isocalendar()
        counts[f"{year}-W{week:02d}"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Owner-side install/usage counting: PyPI download counts (always) plus "
            "the usage-ping collector's own installs/events database (if --db is given)."
        )
    )
    parser.add_argument(
        "--db",
        default=None,
        help=(
            "Path to the usage-ping collector's SQLite database "
            "(hosted/usage_ping/service.py's USAGE_PING_DB). Omit to skip layer 2 entirely."
        ),
    )
    args = parser.parse_args(argv)

    print_pypi_stats()
    if args.db is not None:
        print_collector_stats(Path(args.db))
    else:
        print()
        print("(--db not given -- skipping the usage-ping collector's own stats;")
        print(" pass --db /path/to/usage-ping.sqlite3 to include installs/activation/")
        print(" active-user numbers from a deployed collector.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
