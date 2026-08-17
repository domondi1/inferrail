# inferrail-mcp

An MCP (Model Context Protocol) server exposing Inferrail's local receipt
ledger to MCP-aware agents — Claude Code, Claude Desktop, Cursor, or any
other MCP client. Stdio transport.

It is a thin adapter, not a new execution path: `get_spend` calls the
exact aggregation `inferrail report` already implements
(`inferrail.cli.report.load_receipts`/`aggregate`) over your local
`inferrail-receipts.jsonl`; `get_health` pings the gateway's existing
`/health` endpoint and separately reports the most recent local receipt.
Neither tool can execute inference, spend provider budget, or change
configuration — see each tool's description in `src/inferrail_mcp/server.py`
for the exact contract.

## Tools

| Tool | What it does | Can it cost money or mutate anything? |
|---|---|---|
| `get_spend` | Aggregates local receipts by provider/model/route/attribute, optional time window | No — reads a local file only |
| `get_health` | Checks gateway reachability (`/health`) + most recent local receipt | No — never issues a new inference call |

## Install

```bash
pip install -e ".[mcp]"   # from the inferrail repo root
```

## Run directly (for testing)

```bash
inferrail-mcp
```

Speaks MCP over stdio — not meant to be run interactively; see the client
config snippets in the main [README](../README.md#for-agents).

## Why not more tools

`explain_error` is deferred until stable `INFERRAIL_E###` error codes ship
(see `ERRORS.md`) — there's nothing to explain yet without them.
`configure_budget` is intentionally omitted: no budget/policy primitive
exists in Inferrail today (`docs/PRODUCT.md`'s non-goals), and this server
does not claim capabilities the gateway doesn't have.
