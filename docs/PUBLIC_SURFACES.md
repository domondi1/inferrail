# Public product change checklist

Production-generated contracts remain authoritative for activated services and rails.
[product-catalog.json](product-catalog.json) is a dated inventory of verified public facts,
not a runtime feature flag or an assumption that every merged capability is deployed.
Commercial activation and developer-preview maturity are separate fields.

For a change to an endpoint, price, payment path, guarantee or installable release:

1. Verify the deployed endpoint with an unpaid request and inspect generated OpenAPI, service
   `llms.txt` and pricing. Do not purchase or consume balances to validate marketing copy.
2. Check actual npm/PyPI/Registry versions and Desktop release assets. Source versions are
   insufficient. Coordinate with release work already in progress.
3. Update the inventory's evidence/date and availability; then review the homepage/product
   page, pricing, developer onboarding, README, both `llms.txt` files, affected skill/plugin
   descriptions and production discovery generation. Preserve disabled-rail conditions.
4. Run `python scripts/check_public_surfaces.py` and the applicable service/metadata tests.
   With network access, run `python scripts/check_public_surfaces.py --live`. Investigate
   mismatches rather than automatically enabling a product or changing a price.
5. Review the rendered pages at mobile/tablet/desktop widths. Check the unpaid first request,
   CTA destinations, copy buttons, keyboard navigation and error instructions.
6. Review the full public diff and run `bash scripts/check_no_internal_content.sh` before push.
   Recheck production availability before release; do not merge or deploy unreviewed claims.

The offline validator runs in CI. It checks local links/anchors, image alternatives, headings,
canonical URLs, inventory evidence and essential payment/discovery consistency. It deliberately
does not depend on live services or assert that static documentation proves deployment.
The optional live mode sends no credentials and cannot authorize a purchase.

## Verification baseline: 2026-11-28

- Web Read: live 402 Base/Tempo challenge and 401 API-key route, 2 credits/$0.002.
- Card: live Hosted pricing lists $10/10,000 credits; USDC $1/1,000 credits. No paid checkout
  was completed; Stripe fulfillment was not independently exercised in this workstream.
- Package Check, Domain Check, Finder: live wallet 402s, $0.002/result.
- Package/Domain credits: merged in #260; fresh probes after the concurrent deployment
  return 401 workspace_key_required, 2 credits/$0.002. Both native HTTP paths are live.
  Published npm 0.3.0 remains Web Read-only in credit mode; source 0.3.2 adds the other tools.
- npm 0.3.0, PyPI 0.4.18, MCP Web Read entry 1.2.0 and Desktop 0.3.0 are published.
  npm/Desktop source 0.3.2 and Registry source 1.2.2 are not publication claims. Desktop asset contains SDK 0.3.0.
- Ten public Apify listings expose pay-per-event prices. No Actor run was purchased.
- AWS route 404; extra networks, Dify marketplace release and current third-party directory
  indexing are unverified. Browser start page #262 merged and its GET page is live.
- Existing team affiliations are individual background statements, not endorsements; their
  independent credential verification is outside these API/registry checks. The prior physical
  office address is omitted rather than republished without verification. Email contact remains.
