#!/usr/bin/env bash
# Builds inferrail-web-read.mcpb (a Claude Desktop extension) from the published npm package, so
# the bundle runs exactly what `npx -y @inferrail/ai-sdk-tools` runs. Usage: bash build.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
version="$(node -p "require('$here/manifest.json').version")"
out="$(mktemp -d)"
cp "$here/manifest.json" "$out/"
cp "$here/../../LICENSE" "$out/LICENSE"
cd "$out"
npm init -y >/dev/null
npm install --no-audit --no-fund --omit=dev --ignore-scripts "@inferrail/ai-sdk-tools@$version" >/dev/null
installed="$(node -p "require('./node_modules/@inferrail/ai-sdk-tools/package.json').version")"
[ "$installed" = "$version" ] || { echo "installed $installed, manifest says $version" >&2; exit 1; }
rm -f package.json package-lock.json
npx -y @anthropic-ai/mcpb@2.1.2 validate manifest.json
npx -y @anthropic-ai/mcpb@2.1.2 pack . "$here/inferrail-web-read-$version.mcpb"
echo "$here/inferrail-web-read-$version.mcpb"
