"""The browser start page for prepaid credits: create an API key, then fund it once by card.

Plain HTML, no scripts, every value escaped. The key is shown once on the page that creates it
and the page is never cached. Card payment happens on Stripe's own Checkout page; credits are
added only when Stripe's signed webhook confirms the payment (see stripe_checkout.py).
"""

from __future__ import annotations

from html import escape

STYLE = (
    "body{font:16px/1.5 system-ui,sans-serif;max-width:44rem;margin:2rem auto;padding:0 1rem;"
    "color:#1a1a1a;background:#fff}code,pre{font:14px ui-monospace,monospace;background:#f3f3f3;"
    "border-radius:4px}pre{padding:.75rem;overflow-x:auto}code{padding:.1rem .3rem}"
    ".key{font-size:15px;word-break:break-all;padding:.75rem;border:2px solid #c80;"
    "background:#fff8e6}button,.pay{font:inherit;padding:.6rem 1rem;border-radius:6px;"
    "border:1px solid #333;background:#111;color:#fff;cursor:pointer;text-decoration:none;"
    "display:inline-block}input{font:inherit;padding:.5rem;width:100%;box-sizing:border-box}"
    "@media (prefers-color-scheme:dark){body{background:#111;color:#eee}code,pre{background:#222}"
    ".key{background:#2a2200}button,.pay{background:#eee;color:#111}}"
)


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)}</title><style>{STYLE}</style></head><body>{body}</body></html>"
    )


def pack_line(pack_cents: int, pack_credits: int) -> str:
    pages = pack_credits // 2
    return (
        f"${pack_cents / 100:,.2f} buys {pack_credits:,} credits: {pages:,} Web Read pages at "
        "$0.002 each"
    )


def start(base: str, pack_cents: int, pack_credits: int, card: bool) -> str:
    """The landing page: what credits buy and a button that creates a key."""
    pay = (
        f"<p>{escape(pack_line(pack_cents, pack_credits))}. You pay by card on Stripe's checkout "
        "page; this is the only step a person does. Your agents, workflows and scripts then "
        "spend the balance on their own.</p>"
        if card
        else "<p>Card payments are not enabled on this deployment. Credits can be bought with "
        f"USDC over x402 at <code>POST {escape(base)}/v1/credits/x402</code>.</p>"
    )
    return _page(
        "Inferrail: get an API key",
        "<h1>Get an Inferrail API key</h1>"
        "<p>One key pays for Inferrail services from prepaid credits, with no wallet and no "
        "signup. <strong>Web Read</strong> returns any public web page as clean Markdown with its "
        "title, dates, authors and a content hash for <strong>$0.002 per delivered page</strong>; "
        "a page that can't be read costs nothing.</p>"
        f"{pay}"
        f'<form method="post" action="{escape(base)}/v1/start">'
        "<button type=submit>Create my key</button></form>"
        "<h2>Already have a key?</h2>"
        f'<form method="post" action="{escape(base)}/v1/start/top-up">'
        '<p><input name="api_key" placeholder="irw_..." autocomplete="off" required></p>'
        "<button type=submit>Add credits</button></form>"
        '<p><a href="https://github.com/domondi1/inferrail/tree/main/packages/ai-sdk-tools'
        '#pay-with-an-api-key-prepaid-credits-no-wallet">Docs</a></p>',
    )


def created(base: str, origin: str, api_key: str, checkout_url: str | None, pack_line_: str) -> str:
    """The page that shows a new key once, with the card checkout link and how to use it."""
    pay = (
        f'<p><a class="pay" href="{escape(checkout_url)}">Pay by card: '
        f"{escape(pack_line_)}</a></p><p>Credits appear within seconds of payment. Check the "
        f"balance with <code>GET {escape(base)}/v1/workspace</code>.</p>"
        if checkout_url
        else "<p>Card payments are not enabled here. Buy credits with USDC over x402 at "
        f"<code>POST {escape(base)}/v1/credits/x402</code>.</p>"
    )
    key = escape(api_key)
    read = f"{escape(origin)}/read/credits/v1/page"
    return _page(
        "Inferrail: your API key",
        "<h1>Your API key</h1>"
        "<p><strong>Copy it now. It is shown only once;</strong> Inferrail keeps only its hash.</p>"
        f'<p class="key">{key}</p>'
        f"{pay}"
        "<h2>Use it</h2>"
        "<p>HTTP, from any server, cron job, n8n, Zapier or Make:</p>"
        f"<pre>curl -X POST {read} \\\n  -H 'Authorization: Bearer {key}' \\\n"
        "  -H 'content-type: application/json' \\\n"
        '  -d \'{"url": "https://example.com/"}\'</pre>'
        "<p>MCP clients (Claude Code, Claude Desktop, Cursor): run "
        "<code>npx -y @inferrail/ai-sdk-tools</code> with "
        f"<code>INFERRAIL_API_KEY={key}</code>. Vercel AI SDK: "
        "<code>inferrailCreditTools({ apiKey })</code>. Dify: the <em>Inferrail Web Read</em> "
        "plugin.</p>",
    )


def top_up(checkout_url: str | None, pack_line_: str, base: str) -> str:
    pay = (
        f'<p><a class="pay" href="{escape(checkout_url)}">Pay by card: {escape(pack_line_)}</a></p>'
        if checkout_url
        else "<p>Card payments are not enabled here. Buy credits with USDC over x402 at "
        f"<code>POST {escape(base)}/v1/credits/x402</code>.</p>"
    )
    return _page("Inferrail: add credits", f"<h1>Add credits</h1>{pay}")


def problem(message: str, base: str) -> str:
    return _page(
        "Inferrail: something went wrong",
        f"<h1>Something went wrong</h1><p>{escape(message)}</p>"
        f'<p><a href="{escape(base)}/v1/start">Back</a></p>',
    )


def thanks(base: str) -> str:
    return _page(
        "Inferrail: payment received",
        "<h1>Payment submitted</h1><p>Credits are added as soon as Stripe confirms the payment, "
        "usually within seconds. Check the balance with "
        f"<code>GET {escape(base)}/v1/workspace</code> and your key.</p>",
    )
