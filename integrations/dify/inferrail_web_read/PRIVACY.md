# Privacy policy: Inferrail Web Read for Dify

**What the plugin sends.** For each tool call: the URL(s) you ask it to read, an optional
request id, your Inferrail API key (as an `Authorization` header) and the label
`x-inferrail-source: dify`. It sends them to `https://inferrail-cost-gateway.onrender.com` over
HTTPS. It sends nothing else from your Dify workspace: no prompts, conversation content, files or
user identifiers.

**What Inferrail stores.**
- The workspace key is stored only as a SHA-256 hash.
- Billing records (credits held and billed per request, as a hashed reference) and aggregate
  counters (deliveries, refusals) carry no URL or page content.
- A delivered result is kept for up to the recovery window (at least 10 minutes), so a retry
  with the same request id is answered without a second charge, and then deleted.

**What the plugin stores.** Nothing. The API key is held by Dify as an encrypted credential.

**Third parties.** Inferrail fetches the requested public page from its host. Card payments for
credits are handled by Stripe on Stripe's own checkout page; the plugin never sees card details.

**Contact.** https://github.com/domondi1/inferrail/issues
