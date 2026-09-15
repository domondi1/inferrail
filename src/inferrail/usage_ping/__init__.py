"""The opt-in, anonymous usage ping (docs/adr/0019-opt-in-usage-ping.md).

Off by default. See `usage_ping.client.maybe_send_event` for the one
entry point every integration (gateway, CLI, local API) calls -- it is
always safe to call unconditionally: it no-ops immediately, with no
network access at all, unless the caller has explicitly enabled the
ping *and* configured a real endpoint.
"""

from __future__ import annotations
