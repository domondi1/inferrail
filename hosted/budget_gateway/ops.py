"""Operator commands for Inferrail Hosted's money ledger. Run them on the host (e.g. Render
Shell), alongside the live service.

    python3 hosted/budget_gateway/ops.py backup /var/data/backups
    python3 hosted/budget_gateway/ops.py liabilities
    python3 hosted/budget_gateway/ops.py report [ws_a:test,ws_b:founder] [0xwallet:test]
    python3 hosted/budget_gateway/ops.py reconcile          # needs BG_BASE_RPC_URL + network
    python3 hosted/budget_gateway/ops.py refund-x402 <payer:nonce> <refund tx hash>

- **backup** uses SQLite's online backup API, so the copy is consistent while the service runs.
  Restoring an older copy can re-open spent credits, so reconcile against the chain and Stripe
  after any restore.
- **report** is the funnel, money and liability report (report.py). The optional arguments
  exclude internal workspaces and test wallets from the commercial numbers.
- **refund-x402** records a refund only with proof:
  - first send the USDC back from the receiving wallet;
  - then pass that transaction's hash;
  - the amount is read from the chain: a confirmed USDC transfer from `BG_X402_PAY_TO` to the
    original payer.
  - One transaction backs one refund; re-running it is a no-op. The service itself never
    sends funds.
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
import report as report_mod  # noqa: E402
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
        "net_cash_cents_by_source": ledger.revenue(),
    }


def report(exclude: str = "", exclude_wallets: str = "") -> dict[str, object]:
    return report_mod.build(
        _ledger(),
        exclude_workspaces=report_mod.parse_labels(exclude),
        exclude_wallets=report_mod.parse_labels(exclude_wallets),
    )


def reconcile() -> dict[str, int]:
    rpc, network = os.environ.get("BG_BASE_RPC_URL"), os.environ.get("BG_X402_NETWORK")
    if not rpc or not network:
        raise SystemExit("reconcile needs BG_BASE_RPC_URL and BG_X402_NETWORK")
    return _ledger().reconcile_pending(
        lambda a, n: chain_check.authorization_used(rpc, network, a, n)
    )


def refund_x402(ref: str, tx_hash: str) -> dict[str, object]:
    rpc, network = os.environ.get("BG_BASE_RPC_URL"), os.environ.get("BG_X402_NETWORK")
    pay_to = os.environ.get("BG_X402_PAY_TO")
    if not rpc or not network or not pay_to:
        raise SystemExit("refund-x402 needs BG_BASE_RPC_URL, BG_X402_NETWORK and BG_X402_PAY_TO")
    payer = ref.split(":", 1)[0]
    if ":" not in ref or not payer.startswith("0x"):
        raise SystemExit("ref must be <payer address>:<nonce>, as in the workspace ledger")
    try:
        cents = chain_check.usdc_refund_cents(rpc, network, tx_hash, pay_to, payer)
    except chain_check.RefundProofError as ex:
        raise SystemExit(f"not recorded: {ex}") from ex
    if cents <= 0:
        raise SystemExit("not recorded: the transfer is smaller than one cent")
    try:
        reversed_runs = _ledger().record_refund_proof("x402", ref, tx_hash, cents)
    except ValueError as ex:
        raise SystemExit(f"not recorded: {ex}") from ex
    if reversed_runs is None:
        raise SystemExit(f"no x402 grant with ref {ref}")
    return {
        "ref": ref,
        "tx_hash": tx_hash.lower(),
        "verified_cents": cents,
        "runs_reversed_total": reversed_runs,
    }


def main(argv: list[str]) -> int:
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    handlers = {
        "backup": lambda: backup(*args),
        "liabilities": liabilities,
        "report": lambda: report(*args),
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
