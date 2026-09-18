"""Wraps a `ReceiptSink` to also print one compact, human-readable line per
receipt to the terminal — the "immediately visible" half of the quickstart
promise (the other half is the dashboard Live Feed, see
`docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md`).

Same wrapping pattern as `usage_ping.receipt_hook.UsagePingReceiptSink`:
forwards every `emit()` unchanged, so it can never change what's actually
recorded, only what's printed alongside it. Only installed for
`inferrail serve --quickstart` (`gateway/app.py`) — a self-hosted operator
running a real `inferrail.yaml` deployment doesn't get an extra unrequested
stdout line per request just because this module exists.
"""

from __future__ import annotations

from decimal import Decimal

from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sinks import ReceiptSink


def _format_usd(value: Decimal) -> str:
    # Deliberately duplicated from `cli.report.format_usd` rather than
    # imported: `receipts/` is a lower-level package `cli/` depends on, not
    # the reverse, and this one six-line formatting rule isn't worth an
    # inverted import to share. Keep both in sync if the precision/rounding
    # rule ever changes.
    text = format(value.quantize(Decimal("0.000001")), "f")
    integer_part, _, frac_part = text.partition(".")
    frac_part = frac_part.rstrip("0").ljust(2, "0")
    return f"${integer_part}.{frac_part}"


class ConsoleSummaryReceiptSink:
    def __init__(self, inner: ReceiptSink) -> None:
        self._inner = inner

    def emit(self, receipt: InferenceReceipt) -> None:
        self._inner.emit(receipt)
        print(_summary_line(receipt), flush=True)


def _summary_line(receipt: InferenceReceipt) -> str:
    if receipt.status == "success" and receipt.estimated_cost_usd is not None:
        cost = _format_usd(receipt.estimated_cost_usd)
    elif receipt.status == "success":
        cost = "unknown"
    else:
        cost = "n/a (" + receipt.status + ")"
    tokens = (
        f"{receipt.prompt_tokens}+{receipt.completion_tokens} tok"
        if receipt.prompt_tokens is not None and receipt.completion_tokens is not None
        else "tokens unknown"
    )
    work_id = receipt.attributes.get("work_id")
    work_part = f" work_id={work_id}" if work_id else ""
    return (
        f"receipt  {receipt.provider}/{receipt.model}  {tokens}  cost={cost}{work_part}  "
        f"(no prompt/response ever recorded — {receipt.receipt_id})"
    )
