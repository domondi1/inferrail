"""MCPB entry point for Inferrail's existing stdio MCP server.

Hosts launch the bundle through `server.mcp_config` in manifest.json
(`uvx inferrail@<version> mcp`), which installs the published `inferrail`
package from PyPI. This file exists because MCPB requires an entry point;
it adds no behavior of its own.
"""

from inferrail_mcp.server import main

if __name__ == "__main__":
    main()
