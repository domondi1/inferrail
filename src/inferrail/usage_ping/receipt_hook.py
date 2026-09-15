"""Wraps a `ReceiptSink` to also fire the `first_receipt`/`tool_connected`
usage-ping milestones, without touching `InferenceEngine` or
`AnthropicInferenceEngine` at all.

Every receipt, from either wire format or from `inferrail try`/`ap demo`
run against this store, already flows through one `ReceiptSink.emit()`
call -- wrapping that one call is a much smaller surface than adding a
call site to each engine, and it can never see anything the sink itself
doesn't already see (i.e. never a prompt/response, since `InferenceReceipt`
never carries one -- see docs/adr/0005).

Only installed for `--app-mode` (`gateway/app.py`), matching the other
usage-ping integration points: the Settings toggle that turns this on
only exists on the app-mode dashboard.
"""

from __future__ import annotations

from pathlib import Path

from inferrail.config.models import UsagePingConfig
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sinks import ReceiptSink
from inferrail.usage_ping.client import maybe_send_event


class UsagePingReceiptSink:
    """Forwards every `emit()` to `inner` unchanged, then -- cheaply and
    non-blockingly, see `usage_ping.client.maybe_send_event` -- checks
    whether this receipt is this install's first ever (`first_receipt`)
    and/or its first ever successful one (`tool_connected`, the signal
    that some real client actually completed a request through the
    gateway)."""

    def __init__(
        self, inner: ReceiptSink, *, app_data_dir: Path, config: UsagePingConfig
    ) -> None:
        self._inner = inner
        self._app_data_dir = app_data_dir
        self._config = config

    def emit(self, receipt: InferenceReceipt) -> None:
        self._inner.emit(receipt)
        maybe_send_event("first_receipt", app_data_dir=self._app_data_dir, config=self._config)
        if receipt.status == "success":
            maybe_send_event(
                "tool_connected", app_data_dir=self._app_data_dir, config=self._config
            )
