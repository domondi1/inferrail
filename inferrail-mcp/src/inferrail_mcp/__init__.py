"""MCP adapter exposing Inferrail's local receipt ledger and gateway health
to MCP-aware agents (Claude Code, Claude Desktop, Cursor).

This is a thin adapter, not a new execution path: `get_spend` reuses
`inferrail.cli.report`'s existing aggregation logic verbatim (the same
code `inferrail report` runs), and `get_health` only reads the local
receipts file and pings the gateway's existing `/health` endpoint. See
`inferrail-mcp/README.md`.
"""

from __future__ import annotations
