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
import { call, creditsFetch, ORIGIN, type Fetch } from "./core.js";

export * from "./core.js";

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

export interface InferrailCreditToolsOptions {
  /** An Inferrail workspace key (`irw_...`) with prepaid credits. */
  apiKey: string;
  /** The underlying fetch (tests, proxies). Default: the global fetch. */
  fetch?: Fetch;
  origin?: string;
}

/**
 * Web Read tools paid from prepaid Inferrail credits with an API key: no wallet and no
 * per-request signing. A delivered page costs 2 credits ($0.002); a page that can't be read
 * costs nothing. A person funds the workspace once (card checkout or USDC); the agent then
 * spends the balance on its own.
 */
export function inferrailCreditTools({ apiKey, fetch: base = fetch, origin = ORIGIN }: InferrailCreditToolsOptions) {
  const keyed = creditsFetch(apiKey, base, origin);
  const run = (path: string) => (body: Record<string, unknown>) => call(keyed, path, body, origin);
  return {
    readPage: tool({
      description:
        "Read one public web page as clean markdown with its title, metadata (description, " +
        "canonical URL, authors, published date, language), word count and a content hash. " +
        "Costs $0.002 from prepaid credits, only when the page is delivered.",
      inputSchema: z.object({ url: z.string().url().max(2048).describe("Public http(s) URL") }),
      execute: run("/read/v1/page"),
    }),
    readPages: tool({
      description:
        "Read 1-10 public web pages, $0.002 from prepaid credits per delivered page. Pages that " +
        "can't be read cost nothing and are listed under `failed`.",
      inputSchema: z.object({
        urls: z.array(z.string().url().max(2048)).min(1).max(10).describe("Distinct public URLs"),
      }),
      execute: run("/read/v1/pages"),
    }),
  };
}
