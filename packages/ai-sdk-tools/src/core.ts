/**
 * The framework-free core shared by the AI SDK tools and the MCP server: origin, prices,
 * receiving addresses, the request helper and the price guard. No dependencies.
 */
export const ORIGIN = "https://inferrail-cost-gateway.onrender.com";
/** $0.002 in USDC base units (6 decimals): the price of one page, check, lookup or answer. */
export const PRICE_ATOMIC = 2000n;
/** Each service's receiving address on Base, as its 402 names it. */
export const PAY_TO = {
  read: "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E",
  find: "0x54BF9C490112CD6a54Cb8f22D6bf83621ABe77CF",
  packages: "0xe9c1249648257C1C1D70F8b7682851C377bb8f53",
  domains: "0xb95D56f169324A0B23B82bd3975635Cf0187ce07",
} as const;

/** Path -> (receiving address, most units one request can buy). */
export const ROUTES: Record<string, { payTo: string; maxUnits: bigint }> = {
  "/read/v1/page": { payTo: PAY_TO.read, maxUnits: 1n },
  "/read/v1/pages": { payTo: PAY_TO.read, maxUnits: 10n },
  "/find/v1/find": { payTo: PAY_TO.find, maxUnits: 1n },
  "/pkg/v1/check": { payTo: PAY_TO.packages, maxUnits: 1n },
  "/domain/v1/lookup": { payTo: PAY_TO.domains, maxUnits: 1n },
};

/** Sent with every call so the services can count this channel; nothing about you. */
export const SOURCE_HEADER = { "x-inferrail-source": "ai-sdk" } as const;

export type Fetch = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

export type Result =
  | ({ ok: true } & Record<string, unknown>)
  | {
      ok: false;
      charged: false;
      status: number;
      error: string;
      detail?: string;
      results?: Array<{ url: string; delivered: boolean; error?: string; detail?: string }>;
    };

function randomId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `ai-sdk-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export interface CallOptions {
  /** The integration sending the request, for the services' per-channel counts. */
  source?: string;
  /** What a 402 (not paid) result tells the caller to do. */
  unpaidDetail?: string;
}

/** POST one paid request. Never throws for an HTTP answer: the model gets a result it can use. */
export async function call(
  paidFetch: Fetch,
  path: string,
  body: Record<string, unknown>,
  origin: string = ORIGIN,
  options: CallOptions = {},
): Promise<Result> {
  const payload = JSON.stringify({ ...body, request_id: randomId() });
  const init: RequestInit = {
    method: "POST",
    headers: {
      "content-type": "application/json",
      accept: "application/json",
      "x-inferrail-source": options.source ?? SOURCE_HEADER["x-inferrail-source"],
    },
    body: payload,
  };
  let res: Response;
  try {
    res = await paidFetch(`${origin}${path}`, init);
  } catch {
    // Same request_id: a request that was paid and delivered is answered again, not charged again.
    res = await paidFetch(`${origin}${path}`, init);
  }
  let json: Record<string, unknown> = {};
  try {
    json = (await res.json()) as Record<string, unknown>;
  } catch {
    // a non-JSON answer: reported by status below
  }
  if (res.ok) return { ok: true, ...json };
  if (res.status === 402 && json.error === "insufficient_credits") {
    const topUp = (json.top_up ?? {}) as Record<string, string>;
    return {
      ok: false,
      charged: false,
      status: 402,
      error: "insufficient_credits",
      detail:
        `Not enough Inferrail credits (this needs ${json.credits_needed}, ${json.credits_remaining} left). ` +
        `A person tops up the workspace once: ${topUp.card ?? topUp.x402 ?? "see the docs"}.`,
    };
  }
  if (res.status === 402) {
    return {
      ok: false,
      charged: false,
      status: 402,
      error: String(json.error ?? "payment_required"),
      detail:
        options.unpaidDetail ??
        "Not paid. The fetch given to inferrailTools must pay x402 offers (e.g. wrapFetchWithPayment " +
          "from @x402/fetch with a funded Base USDC wallet).",
    };
  }
  const detail = json.detail ?? json.message;
  return {
    ok: false,
    charged: false,
    status: res.status,
    error: String(json.error ?? `http_${res.status}`),
    ...(detail === undefined ? {} : { detail: String(detail).slice(0, 300) }),
    // A batch refusal lists which URLs failed, so the caller can resend without them.
    ...(Array.isArray(json.results) ? { results: json.results.slice(0, 10) } : {}),
  };
}

/** Minimal shape of an @x402/core x402Client (or x402HTTPClient) that the guard needs. */
export interface HookableClient {
  onBeforePaymentCreation(
    hook: (ctx: {
      paymentRequired: { resource?: { url?: string } };
      selectedRequirements: { amount: string; payTo: string };
    }) => Promise<void | { abort: true; reason: string }>,
  ): unknown;
}

/** The guard's decision for one offer (exported for tests and for clients with other hooks). */
export function checkOffer(
  resourceUrl: string | undefined,
  requirements: { amount: string; payTo: string },
  origin: string = ORIGIN,
): void | { abort: true; reason: string } {
  if (!resourceUrl?.startsWith(`${origin}/`)) return; // another seller: not ours to judge
  const path = new URL(resourceUrl).pathname;
  const route = ROUTES[path];
  if (!route) return { abort: true, reason: `inferrail: unknown paid route ${path}` };
  if (requirements.payTo.toLowerCase() !== route.payTo.toLowerCase()) {
    return { abort: true, reason: `inferrail: ${path} must pay ${route.payTo}` };
  }
  let amount: bigint;
  try {
    amount = BigInt(requirements.amount);
  } catch {
    return { abort: true, reason: "inferrail: unreadable amount" };
  }
  if (amount <= 0n || amount > PRICE_ATOMIC * route.maxUnits) {
    return { abort: true, reason: `inferrail: ${path} costs at most ${PRICE_ATOMIC * route.maxUnits}` };
  }
}

/** Make `client` refuse any Inferrail offer above the listed price or to another address. */
export function guardInferrailPrices<T extends HookableClient>(client: T, origin: string = ORIGIN): T {
  client.onBeforePaymentCreation(async (ctx) =>
    checkOffer(ctx.paymentRequired.resource?.url, ctx.selectedRequirements, origin),
  );
  return client;
}

/** Web Read's API-key route: pages paid from prepaid workspace credits (2 credits = $0.002). */
export const CREDITS_PAGE_PATH = "/read/credits/v1/page";

/**
 * A fetch that pays Web Read from prepaid Inferrail credits with a workspace key (`irw_...`)
 * instead of signing a payment per request. Page reads go to the credits route; a batch
 * (`/read/v1/pages`) is read page by page, so each delivered page is charged and failures are
 * listed. Other services have no credits route: their requests pass through unpaid (402).
 */
export function creditsFetch(apiKey: string, base: Fetch = fetch, origin: string = ORIGIN): Fetch {
  if (!apiKey.startsWith("irw_")) throw new Error("an Inferrail workspace key starts with irw_");
  const auth = { authorization: `Bearer ${apiKey}` };
  const page = (init: RequestInit, body: Record<string, unknown>) =>
    base(`${origin}${CREDITS_PAGE_PATH}`, {
      ...init,
      headers: { ...(init.headers as Record<string, string>), ...auth },
      body: JSON.stringify(body),
    });
  return async (input, init = {}) => {
    const url = String(input instanceof Request ? input.url : input);
    if (url === `${origin}/read/v1/page`) {
      return page(init, JSON.parse(String(init.body ?? "{}")));
    }
    if (url === `${origin}/read/v1/pages`) {
      const body = JSON.parse(String(init.body ?? "{}")) as { urls?: string[]; request_id?: string };
      const urls = body.urls ?? [];
      const answers = await Promise.all(
        urls.map(async (u, i) => {
          const r = await page(init, { url: u, request_id: `${body.request_id ?? "batch"}:${i}` });
          return { url: u, status: r.status, json: (await r.json().catch(() => ({}))) as Record<string, unknown> };
        }),
      );
      const short = answers.find((a) => a.status === 402);
      if (short && !answers.some((a) => a.status === 200)) {
        return new Response(JSON.stringify(short.json), { status: 402 });
      }
      const pages = answers.filter((a) => a.status === 200).map((a) => a.json);
      const failed = answers
        .filter((a) => a.status !== 200)
        .map((a) => ({ url: a.url, error: String(a.json.error ?? `http_${a.status}`) }));
      const out = { pages, count: pages.length, failed, charged_pages: pages.length };
      return new Response(JSON.stringify(out), { status: pages.length ? 200 : 422 });
    }
    return base(input, init);
  };
}
