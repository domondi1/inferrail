/**
 * Inferrail Web Read for agents that already pay with x402 (@x402/fetch, @x402/core).
 *
 * Bring your own payment-enabled fetch: this module never sees a key. It adds the Web Read
 * contract on top of it:
 *
 * - `guardWebReadPrice(client)`: your x402Client refuses to sign a Web Read offer above the
 *   listed $0.002 or to any address but Web Read's. Other sellers' offers are untouched.
 * - `readPage(paidFetch, url)`: one page as markdown. Every call carries a request_id; a network
 *   failure is retried with the same id, which Web Read answers with the paid page and no second
 *   charge. Refusals (robots.txt, 4xx, non-text, too large...) throw `PageNotBought` and are never
 *   charged.
 * - `quote()`: the unpaid offer, for an agent that wants to check the price before buying.
 * - `webReadTool`: a JSON-schema tool definition (OpenAI, Anthropic, Vercel AI SDK, MCP clients).
 */

export const WEB_READ_ENDPOINT = "https://inferrail-cost-gateway.onrender.com/read/v1/page";
/** Web Read's receiving address on Base, as its 402 names it. */
export const WEB_READ_PAY_TO = "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E";
/** $0.002 in USDC base units (6 decimals). */
export const WEB_READ_PRICE_ATOMIC = 2000n;

export interface Page {
  url: string;
  final_url: string;
  title: string;
  content_markdown: string;
  word_count: number;
  content_sha256: string;
  fetched_at: string;
  receipt?: { payment_ref?: string; price_usd?: string; request_id?: string | null };
  [key: string]: unknown;
}

/** The page wasn't delivered, so nothing was charged. `retryable` is true for 502/504. */
export class PageNotBought extends Error {
  readonly charged = false;
  readonly retryable: boolean;
  constructor(
    readonly status: number,
    readonly code: string,
    readonly detail: string,
  ) {
    super(`Web Read refused (${status} ${code}${detail ? `: ${detail}` : ""}); not charged`);
    this.name = "PageNotBought";
    this.retryable = status === 502 || status === 504;
  }
}

type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

/** Minimal shape of an @x402/core x402Client (or x402HTTPClient) that this needs. */
export interface HookableClient {
  onBeforePaymentCreation(
    hook: (ctx: {
      paymentRequired: { resource?: { url?: string } };
      selectedRequirements: { amount: string; payTo: string };
    }) => Promise<void | { abort: true; reason: string }>,
  ): unknown;
}

/**
 * Refuse, before signing, any Web Read offer above `maxAtomic` or paid to another address.
 * Applies only to requests for `endpoint`; other sellers' offers pass through untouched.
 */
export function guardWebReadPrice<C extends HookableClient>(
  client: C,
  { maxAtomic = WEB_READ_PRICE_ATOMIC, endpoint = WEB_READ_ENDPOINT, payTo = WEB_READ_PAY_TO } = {},
): C {
  const target = new URL(endpoint);
  client.onBeforePaymentCreation(async ({ paymentRequired, selectedRequirements }) => {
    const raw = paymentRequired.resource?.url;
    if (!raw) return;
    const url = new URL(raw);
    if (url.origin !== target.origin || url.pathname !== target.pathname) return;
    if (selectedRequirements.payTo.toLowerCase() !== payTo.toLowerCase())
      return { abort: true, reason: `Web Read offer pays ${selectedRequirements.payTo}, expected ${payTo}` };
    if (BigInt(selectedRequirements.amount) > maxAtomic)
      return { abort: true, reason: `Web Read offer ${selectedRequirements.amount} exceeds ${maxAtomic}` };
  });
  return client;
}

export interface ReadPageOptions {
  /** Your id for this purchase; reuse it to recover a lost paid response. Default: random. */
  requestId?: string;
  endpoint?: string;
  /** Retries after a network failure (same request_id, so never a second charge). Default 1. */
  retries?: number;
  signal?: AbortSignal;
}

/** Buy one public web page as markdown. Pass a fetch wrapped by @x402/fetch's wrapFetchWithPayment. */
export async function readPage(paidFetch: Fetch, url: string, opts: ReadPageOptions = {}): Promise<Page> {
  const requestId = opts.requestId ?? crypto.randomUUID();
  const body = JSON.stringify({ url, request_id: requestId });
  const retries = opts.retries ?? 1;
  let lastError: unknown;
  for (let attempt = 0; attempt <= retries; attempt++) {
    let res: Response;
    try {
      res = await paidFetch(opts.endpoint ?? WEB_READ_ENDPOINT, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body,
        signal: opts.signal,
      });
    } catch (err) {
      if (opts.signal?.aborted || !isNetworkError(err)) throw err;
      lastError = err;
      continue;
    }
    if (res.ok) return (await res.json()) as Page;
    const data = (await res.json().catch(() => ({}))) as { error?: string; detail?: string };
    if (res.status === 402)
      throw new PageNotBought(402, data.error ?? "payment_required", "payment not accepted; check the client's wallet and network");
    throw new PageNotBought(res.status, data.error ?? `http_${res.status}`, data.detail ?? "");
  }
  throw lastError;
}

function isNetworkError(err: unknown): boolean {
  return err instanceof TypeError || (err instanceof Error && /fetch failed|ECONNRESET|ETIMEDOUT|socket/i.test(err.message));
}

export interface Quote {
  network: string;
  asset: string;
  amount: string;
  payTo: string;
  /** All payment options the 402 offered (x402), for clients that pay on another network. */
  accepts: Array<Record<string, unknown>>;
}

/** The unpaid offer for one read (no signing, no payment). */
export async function quote(fetchImpl: Fetch = fetch, endpoint = WEB_READ_ENDPOINT): Promise<Quote> {
  const res = await fetchImpl(endpoint, { method: "POST", headers: { "content-type": "application/json" }, body: "{}" });
  const header = res.headers.get("payment-required");
  if (res.status !== 402 || !header) throw new Error(`expected a 402 offer, got ${res.status}`);
  const offer = JSON.parse(Buffer.from(header, "base64").toString("utf8")) as { accepts: Array<Record<string, unknown>> };
  const first = offer.accepts[0] as { network: string; asset: string; amount: string; payTo: string };
  return { network: first.network, asset: first.asset, amount: first.amount, payTo: first.payTo, accepts: offer.accepts };
}

/** A tool definition agents can register as-is (JSON Schema parameters). */
export const webReadTool = {
  name: "read_page",
  description:
    "Read one public web page as clean markdown (title, main text, headings, lists, tables, links). " +
    "Costs $0.002 in USDC per delivered page, paid from the agent's own x402 wallet; refused or failing " +
    "pages are never charged.",
  parameters: {
    type: "object",
    properties: { url: { type: "string", description: "Public http(s) URL to read", maxLength: 2048 } },
    required: ["url"],
    additionalProperties: false,
  },
} as const;

/** Run the tool: the page on success, or a not-charged refusal the model can read and act on. */
export async function runWebReadTool(paidFetch: Fetch, args: { url: string }) {
  try {
    const page = await readPage(paidFetch, args.url);
    return { ok: true as const, title: page.title, url: page.final_url, markdown: page.content_markdown };
  } catch (err) {
    if (err instanceof PageNotBought)
      return { ok: false as const, error: err.code, detail: err.detail, retryable: err.retryable, charged: false };
    throw err;
  }
}
