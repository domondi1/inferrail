---
name: inferrail-package-check
description: Check package versions for known vulnerabilities before you add, upgrade, pin or ship a dependency, paying $0.002 per check of up to 20 packages from a wallet your agent already has (AgentCash, Coinbase agentic wallet, or any x402/MPP client). Use when a coding or build task adds or changes npm, PyPI, Go, crates.io, Maven, NuGet, RubyGems or Packagist dependencies; returns advisory ids, CVEs, severity, fixed versions and links from OSV.dev, and an OSV outage is never charged.
---

# Inferrail Package Check

Known vulnerabilities for the exact package versions you are about to use, from OSV.dev. One call
checks up to 20 packages for $0.002. You pay only for a delivered check, and a retry with the
same `request_id` never pays twice.

## When to call it

- Before adding a dependency, or bumping one to a new version.
- Before shipping a lockfile change or a release.
- When a user asks "is this version safe?" or "which version fixes this CVE?".

Batch the packages you are changing into one call (up to 20).

## How to call it

`POST https://inferrail-cost-gateway.onrender.com/pkg/v1/check` with

```json
{"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}],
 "request_id": "<unique id>"}
```

Ecosystems: `npm`, `PyPI`, `Go`, `crates.io`, `Maven`, `NuGet`, `RubyGems`, `Packagist`, `Hex`,
`Pub`, `Hackage`, `CRAN`, `SwiftURL`.

With the AgentCash wallet:

```bash
npx agentcash fetch https://inferrail-cost-gateway.onrender.com/pkg/v1/check -m POST \
  -H "Content-Type: application/json" \
  -b '{"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}]}' --max-amount 0.01
```

(Or once: `npx agentcash add https://inferrail-cost-gateway.onrender.com` to list Inferrail's
services in your agent's paid tools.)

With a Coinbase agentic wallet:

```bash
npx awal x402 pay https://inferrail-cost-gateway.onrender.com/pkg/v1/check -X POST \
  -d '{"packages": [{"ecosystem": "npm", "name": "lodash", "version": "4.17.15"}]}'
```

Any other x402 or MPP client works the same way: the first response is HTTP 402 with the offer;
check it is $0.002 before you sign.

## Reading the answer

Each result has a `status`:

- `vulnerable`: `vulns` lists each advisory with `id`, `aliases` (CVE), `summary`, `severity`,
  `fixed` (versions that fix it) and `url`. Upgrade to a version in `fixed`.
- `no_known_vulnerabilities`: OSV.dev had no matching advisory when checked. **This is not a
  guarantee that the package is secure.**
- `incomplete` (`complete: false`): OSV didn't return the whole list; treat it as unchecked.

Each advisory carries its source `database` and `license` (mostly CC-BY 4.0): keep the link and
source when you show results to a user.

## Never charged

- 502 `upstream_failed`: OSV.dev was unreachable. Retry later.
- 422: invalid request (unknown ecosystem, empty list or more than 20 packages).

Guide: https://inferrail-cost-gateway.onrender.com/pkg/llms.txt. Contract:
https://inferrail-cost-gateway.onrender.com/openapi.json.
