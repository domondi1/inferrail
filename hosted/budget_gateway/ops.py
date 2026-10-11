"""Operator commands for Inferrail Hosted's money ledger. Run them on the host (e.g. Render
Shell), alongside the live service.

    python3 hosted/budget_gateway/ops.py backup /var/data/backups
    python3 hosted/budget_gateway/ops.py liabilities
    python3 hosted/budget_gateway/ops.py reconcile          # needs BG_BASE_RPC_URL + network
    python3 hosted/budget_gateway/ops.py refund-x402 <payer:nonce> <cents>

- **backup** uses SQLite's online backup API, so the copy is consistent while the service runs.
  Restoring an older copy can re-open spent credits, so reconcile against the chain and Stripe
  after any restore.
- **refund-x402** records the reversal *after* the operator has sent the USDC back from the
  receiving wallet. The service itself never sends funds.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import chain_check  # noqa: E402
from workspaces import WorkspaceLedger  # noqa: E402


def _ledger() -> WorkspaceLedger:
    data = Path(os.environ.get("BG_DATA_DIR", "/tmp/inferrail_budget_gateway"))
    return WorkspaceLedger(
        data / "workspaces.sqlite3",
        free_runs_per_month=int(os.environ.get("BG_FREE_RUNS_PER_MONTH", "250")),
    )


def backup(dest_dir: str) -> dict[str, str]:
    data = Path(os.environ.get("BG_DATA_DIR", "/tmp/inferrail_budget_gateway"))
    target = Path(dest_dir) / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target.mkdir(parents=True, exist_ok=True)
    copied = {}
    for src in sorted(data.glob("*.sqlite3")):
        with sqlite3.connect(src) as s, sqlite3.connect(target / src.name) as d:
            s.backup(d)
        copied[src.name] = str(target / src.name)
    return copied


def liabilities() -> dict[str, object]:
    ledger = _ledger()
    return {
        "outstanding_liability_cents": ledger.outstanding_liability_cents(),
        "pending_purchases": ledger.pending(),
        "net_revenue_cents_by_source": ledger.revenue(),
    }


def reconcile() -> dict[str, int]:
    rpc, network = os.environ.get("BG_BASE_RPC_URL"), os.environ.get("BG_X402_NETWORK")
    if not rpc or not network:
        raise SystemExit("reconcile needs BG_BASE_RPC_URL and BG_X402_NETWORK")
    return _ledger().reconcile_pending(
        lambda a, n: chain_check.authorization_used(rpc, network, a, n)
    )


def refund_x402(ref: str, cents: str) -> dict[str, object]:
    reversed_runs = _ledger().apply_refund("x402", ref, int(cents))
    if reversed_runs is None:
        raise SystemExit(f"no x402 grant with ref {ref}")
    return {"ref": ref, "runs_reversed_total": reversed_runs}


def main(argv: list[str]) -> int:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    handlers = {
        "backup": lambda: backup(*args),
        "liabilities": liabilities,
        "reconcile": reconcile,
        "refund-x402": lambda: refund_x402(*args),
    }
    if cmd not in handlers:
        print(__doc__)
        return 2
    print(json.dumps(handlers[cmd](), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
