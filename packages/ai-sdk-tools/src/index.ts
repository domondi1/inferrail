/**
 * Inferrail's pay-per-call tools for the Vercel AI SDK: read web pages, check package
 * versions for known vulnerabilities, look up a domain's DNS and registration, and find a
 * paid API for a task. $0.002 USDC per delivered result, paid over x402 by the fetch you pass.
 *
 * Bring your own payment-enabled fetch (for example `wrapFetchWithPayment` from @x402/fetch):
 * this package never sees a key or signs anything. What it adds:
 *
 * - `inferrailTools({ fetch })`: AI SDK tools (`tool()` from "ai") for each service. Every call
 *   carries a request_id; a network failure is retried once with the same id, which the
 *   service answers with the paid result and no second charge.
 * - Refusals and failures come back to the model as `{ ok: false, charged: false, ... }`: the
 *   services charge only for a delivered result.
 * - `guardInferrailPrices(client)`: your x402 client refuses to sign an offer from Inferrail's
 *   origin above the listed price or to any address but the service's own. Offers from other
 *   sellers are untouched.
 */

import { tool } from "ai";
import { z } from "zod";

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
  | { ok: false; charged: false; status: number; error: string; detail?: string };

function randomId(): string {
  return globalThis.crypto?.randomUUID?.() ?? `ai-sdk-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

/** POST one paid request. Never throws for an HTTP answer: the model gets a result it can use. */
export async function call(
  paidFetch: Fetch,
  path: string,
  body: Record<string, unknown>,
  origin: string = ORIGIN,
): Promise<Result> {
  const payload = JSON.stringify({ ...body, request_id: randomId() });
  const init: RequestInit = {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json", ...SOURCE_HEADER },
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
  if (res.status === 402) {
    return {
      ok: false,
      charged: false,
      status: 402,
      error: String(json.error ?? "payment_required"),
      detail:
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
  };
}

const ECOSYSTEMS = [
  "npm", "PyPI", "Go", "crates.io", "Maven", "NuGet", "RubyGems", "Packagist", "Hex", "Pub",
  "Hackage", "CRAN", "SwiftURL",
] as const;
const RECORDS = ["A", "AAAA", "CNAME", "MX", "NS", "TXT", "SOA", "CAA"] as const;

export interface InferrailToolsOptions {
  /** A fetch that pays x402 offers. Unpaid fetch works too: every call then returns a 402 result. */
  fetch: Fetch;
  /** Override the origin (tests, a self-hosted copy). */
  origin?: string;
}

/** The AI SDK tools. Pass them to generateText/streamText as `tools`, or pick the ones you need. */
export function inferrailTools({ fetch: paidFetch, origin = ORIGIN }: InferrailToolsOptions) {
  const run = (path: string) => (body: Record<string, unknown>) => call(paidFetch, path, body, origin);
  return {
    readPage: tool({
      description:
        "Read one public web page as clean markdown with its title, metadata (description, " +
        "canonical URL, authors, published date, language), word count and a content hash. " +
        "Costs $0.002 USDC, only when the page is delivered; blocked, missing or bot-checked " +
        "pages and robots.txt refusals are free.",
      inputSchema: z.object({ url: z.string().url().max(2048).describe("Public http(s) URL") }),
      execute: run("/read/v1/page"),
    }),
    readPages: tool({
      description:
        "Read 1-10 public web pages in one call, $0.002 USDC per page, all or nothing: if any " +
        "page can't be read, nothing is charged and the result says which.",
      inputSchema: z.object({
        urls: z.array(z.string().url().max(2048)).min(1).max(10).describe("Distinct public URLs"),
      }),
      execute: run("/read/v1/pages"),
    }),
    checkPackages: tool({
      description:
        "Check up to 20 package versions for known vulnerabilities (OSV.dev: advisory ids, " +
        "CVEs, severity, fixed versions) before adding, upgrading or shipping a dependency. " +
        "$0.002 USDC per check. A clean result is not a guarantee.",
      inputSchema: z.object({
        packages: z
          .array(
            z.object({
              ecosystem: z.enum(ECOSYSTEMS),
              name: z.string().min(1).max(214),
              version: z.string().min(1).max(128).describe("Exact version"),
            }),
          )
          .min(1)
          .max(20),
      }),
      execute: run("/pkg/v1/check"),
    }),
    lookupDomain: tool({
      description:
        "Look up a domain's DNS records (A, AAAA, CNAME, MX, NS, TXT, SOA, CAA), SPF and DMARC, " +
        "and registration (registrar, status, created and expiry dates, nameservers, DNSSEC). " +
        "$0.002 USDC per lookup. Use before trusting a link, sender or counterparty.",
      inputSchema: z.object({
        domain: z.string().min(3).max(253),
        records: z.array(z.enum(RECORDS)).min(1).max(8).optional(),
        registration: z.boolean().optional(),
      }),
      execute: run("/domain/v1/lookup"),
    }),
    findPaidApi: tool({
      description:
        "Find pay-per-call APIs (x402) that do a task, ranked, with price, network and how to " +
        "call them. $0.002 USDC per answer; no match costs nothing.",
      inputSchema: z.object({
        need: z.string().min(2).max(300).describe("The task, in plain words"),
        max_price_usd: z.number().min(0).optional(),
        network: z.string().max(64).optional().describe("CAIP-2, e.g. eip155:8453"),
        limit: z.number().int().min(1).max(5).optional(),
      }),
      execute: run("/find/v1/find"),
    }),
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
