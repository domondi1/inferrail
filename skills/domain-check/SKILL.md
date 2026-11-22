---
name: inferrail-domain-check
description: Look up a domain's DNS records (A, AAAA, CNAME, MX, NS, TXT, SOA, CAA), SPF and DMARC email policies, and registration (registrar, status, created and expiry dates, domain age, nameservers, DNSSEC) for $0.002 per lookup, paid from a wallet your agent already has (AgentCash or any MPP or x402 client). Use before trusting a link, sender or counterparty, when setting up or debugging DNS or email, or when checking who registered a name and when; a resolver outage is never charged.
---

# Inferrail Domain Check

One call returns what an agent usually needs to know about a domain: its live DNS records, its
email authentication (SPF, DMARC policy, MX count) and its registration record from the
registry's RDAP service. It costs $0.002. You pay only for a delivered lookup, and a retry with
the same `request_id` never pays twice.

## When to call it

- Before you trust a link, an email sender or a counterparty's website: how old is the domain,
  who is the registrar, when does it expire, does it publish SPF and DMARC?
- When you set up or debug DNS or email delivery (MX, TXT, CAA, nameservers).
- When a user asks who registered a name, or whether it is registered at all.

## How to call it

`POST https://inferrail-cost-gateway.onrender.com/domain/v1/lookup` with

```json
{"domain": "example.com", "request_id": "<unique id>"}
```

Optional: `"records"`: a subset of `A`, `AAAA`, `CNAME`, `MX`, `NS`, `TXT`, `SOA`, `CAA`
(default all), and `"registration": false` to skip the registry lookup. Internationalized names
are accepted and answered in their ASCII (`xn--`) form.

With the AgentCash wallet:

```bash
npx agentcash fetch https://inferrail-cost-gateway.onrender.com/domain/v1/lookup -m POST \
  -H "Content-Type: application/json" -b '{"domain": "example.com"}' --max-amount 0.01
```

(Or once: `npx agentcash add https://inferrail-cost-gateway.onrender.com` to list Inferrail's
services in your agent's paid tools.)

Any other MPP or x402 client works the same way: the first response is HTTP 402 and lists each
payment rail this service accepts (MPP on Tempo; x402 on Base when it is listed in the 402).
Check that the price is $0.002 before you sign.

## Reading the answer

- `resolves`: whether the name has DNS records. `dns.<TYPE>.status` is `ok`, `no_records`,
  `nxdomain` (the name does not exist) or `error` (that query failed).
- `email`: `spf`, `dmarc`, `dmarc_policy` (`none`, `quarantine` or `reject`) and `mx_count`.
- `registration_status`:
  - `ok`: `registration` has `registrar`, `status`, `created`, `expires`, `age_days`,
    `nameservers`, `dnssec` and the registry's `terms_url`.
  - `not_registered`: the registry had no record when checked. This is not a guarantee that
    the name can be registered.
  - `not_covered`: that TLD's registry is not queried. Covered: .com, .net, .org, .ai, .info and
    about 450 more TLDs. **This says nothing about whether the name is registered.**
  - `skipped` (you sent `"registration": false`) or `error` (the registry failed; unknown).
- `complete: false`: some part failed and is marked `error`. It is never shown as empty.

Registrant contacts are never returned. Registry data is for lawful use only: don't use it for
bulk harvesting or unsolicited marketing.

## Never charged

- 502 `upstream_failed`: the DNS resolver was unreachable. Retry later.
- 422: not a valid public domain name (a URL, an IP address, or a private name such as
  `localhost`, `.local` or `.internal`).

Guide: https://inferrail-cost-gateway.onrender.com/domain/llms.txt. Contract:
https://inferrail-cost-gateway.onrender.com/openapi.json.
