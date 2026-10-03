# MCPB bundle (Smithery distribution, experimental)

Packages Inferrail's existing stdio MCP server (`inferrail mcp`) as an
[MCP Bundle](https://github.com/modelcontextprotocol/mcpb) for
distribution on [Smithery](https://smithery.ai). Packaging only: the
bundle runs the published `inferrail` package from PyPI, reads receipts
from the user's machine, and adds no hosted endpoint.

## How it runs

`manifest.json` launches `uvx --with "mcp>=2.0" inferrail@<version> mcp`. Nothing is
vendored into the bundle; `server.py` exists only because MCPB requires
an entry point.

This is a Smithery compatibility workaround, not a spec-perfect bundle.
MCPB's `uv` server type fits a PyPI package better, but Smithery's CLI
only accepts `python`, `node`, `binary`, and `bun` bundles. MCPB says a
`python` bundle should vendor its dependencies; this one relies on the
user having [uv](https://docs.astral.sh/uv/) installed instead.

`receipts_path` is optional and maps to `INFERRAIL_RECEIPTS_PATH`. Its
default, `./inferrail-receipts.jsonl`, is Inferrail's own default, so
leaving it unset behaves exactly like `inferrail mcp` today.

## Prerequisites

Node.js 20+ (for the MCPB and Smithery CLIs) and uv.

## Build, validate, inspect

From the repo root:

```bash
npx -y @anthropic-ai/mcpb@2.1.2 validate mcpb/manifest.json
npx -y @anthropic-ai/mcpb@2.1.2 pack mcpb dist/inferrail.mcpb
npx -y @anthropic-ai/mcpb@2.1.2 info dist/inferrail.mcpb
```

`dist/` is gitignored; don't commit the `.mcpb` file.

## Test locally

Run the exact command the bundle launches, against your own receipts:

```bash
INFERRAIL_RECEIPTS_PATH=/absolute/path/to/inferrail-receipts.jsonl uvx --with "mcp>=2.0" inferrail@0.4.12 mcp
```

It speaks MCP over stdio. To try it in Claude Code:

```bash
claude mcp add inferrail -e INFERRAIL_RECEIPTS_PATH=/absolute/path/to/inferrail-receipts.jsonl -- uvx --with "mcp>=2.0" inferrail@0.4.12 mcp
```

## Releasing a new version

The manifest pins the package version. On each release, update both
`version` and the `inferrail@<version>` argument in `manifest.json`,
rebuild, and republish; existing installs stay on the old version until
then.

## Publish to Smithery (maintainers only)

```bash
npm install -g smithery@latest
smithery auth login
smithery mcp publish dist/inferrail.mcpb -n <smithery-namespace>/inferrail
```
