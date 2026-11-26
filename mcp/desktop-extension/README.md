# Inferrail Web Read: Claude Desktop extension (`.mcpb`)

A one-click Claude Desktop extension for the local MCP server in
[`@inferrail/ai-sdk-tools`](../../packages/ai-sdk-tools/README.md) (`inferrail-mcp`). Claude
Desktop asks for two settings when you install it:

- **Wallet private key:** marked `sensitive`, so Claude Desktop keeps it in the OS keychain, not
  in a config file. Use a separate wallet that holds only a little USDC on Base. Leave it empty
  to try the tools without paying.
- **Daily budget (USD):** default 0.10. A call that would pass it is refused before anything is
  signed.

Each delivered result costs $0.002. A page that can't be read, a failed check or a refused call
costs nothing.

## Build

```bash
bash build.sh   # -> inferrail-web-read-<version>.mcpb
```

The script installs the **published** `@inferrail/ai-sdk-tools@<manifest version>` from npm,
checks the installed version, then runs `mcpb validate` and `mcpb pack` (`@anthropic-ai/mcpb`
2.1.2). The bundle therefore runs exactly what `npx -y @inferrail/ai-sdk-tools` runs. Bump
`version` in `manifest.json` with each package release.

## Checked (0.2.1)

- `mcpb validate`: the manifest is valid. `mcpb pack`: 12.3 MB.
- Unpacked and started the way Claude Desktop starts it (`node <dir>/node_modules/@inferrail/ai-sdk-tools/dist/mcp.js`
  with only the manifest's env):
  - Paying mode: x402 and viem load from the bundle, and `tools/list` returns 4 tools.
  - A call over the daily budget answers `daily_budget` before signing.
  - With an empty key (the user left it blank), the server starts in not-paid mode.
