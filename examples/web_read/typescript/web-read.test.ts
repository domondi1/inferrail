import { describe, expect, test } from "bun:test";
import { x402Client } from "@x402/core/client";
import { toClientEvmSigner } from "@x402/evm";
import { registerExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment } from "@x402/fetch";
import { privateKeyToAccount } from "viem/accounts";
import {
  PageNotBought,
  WEB_READ_ENDPOINT,
  WEB_READ_PAY_TO,
  guardWebReadPrice,
  quote,
  readPage,
  runWebReadTool,
} from "./web-read.ts";

// Real @x402/core client, real @x402/fetch wrapper and real EIP-3009 signing with a throwaway
// key, against an in-memory stand-in for Web Read. No network, no funds.
const signer = toClientEvmSigner(privateKeyToAccount(`0x${"11".repeat(32)}`));
const USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";

function offer(resource: string, amount = "2000", payTo = WEB_READ_PAY_TO) {
  return {
    x402Version: 2,
    error: "Payment required",
    resource: { url: resource, description: "Web Read", mimeType: "application/json" },
    accepts: [{ scheme: "exact", network: "eip155:8453", asset: USDC_BASE, amount, payTo,
                maxTimeoutSeconds: 300, extra: { name: "USD Coin", version: "2" } }],
  };
}

const PAGE = { url: "https://news.test/a", final_url: "https://news.test/a", title: "A",
  content_markdown: "# A\n\nbody", word_count: 2, content_sha256: "ab", fetched_at: "2026-11-22T00:00:00Z" };

type Reply = (body: Record<string, unknown>, paid: boolean) => Response | "drop";

/** A fake Web Read: unpaid -> 402 offer; paid -> `reply`. Records what it saw. */
function fakeWebRead(reply: Reply, opts: { resource?: string; amount?: string; payTo?: string } = {}) {
  const seen: Array<{ paid: boolean; body: Record<string, unknown> }> = [];
  const fetchImpl = async (input: RequestInfo | URL, init?: RequestInit) => {
    const req = new Request(input, init);
    const body = JSON.parse((await req.text()) || "{}");
    const paid = req.headers.has("payment-signature");
    seen.push({ paid, body });
    if (!paid) {
      const header = Buffer.from(JSON.stringify(offer(opts.resource ?? WEB_READ_ENDPOINT, opts.amount, opts.payTo))).toString("base64");
      return new Response("{}", { status: 402, headers: { "payment-required": header, "content-type": "application/json" } });
    }
    const r = reply(body, paid);
    if (r === "drop") throw new TypeError("fetch failed");
    return r;
  };
  return { fetchImpl, seen };
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

function payingClient() {
  const client = new x402Client();
  registerExactEvmScheme(client, { signer });
  return guardWebReadPrice(client);
}

describe("readPage with @x402/fetch", () => {
  test("pays once and returns the page, sending a request_id", async () => {
    const site = fakeWebRead(() => json(200, { ...PAGE, receipt: { price_usd: "0.002" } }));
    const page = await readPage(wrapFetchWithPayment(site.fetchImpl, payingClient()), "https://news.test/a");
    expect(page.title).toBe("A");
    expect(site.seen.filter((s) => s.paid)).toHaveLength(1);
    expect(typeof site.seen[0].body.request_id).toBe("string");
  });

  test("a lost response is retried with the same request_id", async () => {
    let paidCalls = 0;
    const site = fakeWebRead(() => (++paidCalls === 1 ? "drop" : json(200, PAGE)));
    const page = await readPage(wrapFetchWithPayment(site.fetchImpl, payingClient()), "https://news.test/a", { requestId: "job-7" });
    expect(page.content_markdown).toContain("body");
    const ids = site.seen.filter((s) => s.paid).map((s) => s.body.request_id);
    expect(ids).toEqual(["job-7", "job-7"]); // Web Read returns the paid page; no second charge
  });

  test("a refusal throws PageNotBought and is marked not charged", async () => {
    const site = fakeWebRead(() => json(422, { error: "disallowed_by_robots_txt", detail: "x", charged: false }));
    const err = await readPage(wrapFetchWithPayment(site.fetchImpl, payingClient()), "https://news.test/p").catch((e) => e);
    expect(err).toBeInstanceOf(PageNotBought);
    expect([err.status, err.code, err.charged, err.retryable]).toEqual([422, "disallowed_by_robots_txt", false, false]);
  });

  test("a timeout is retryable", async () => {
    const site = fakeWebRead(() => json(504, { error: "timeout", charged: false }));
    const err = await readPage(wrapFetchWithPayment(site.fetchImpl, payingClient()), "https://slow.test/").catch((e) => e);
    expect(err.retryable).toBe(true);
  });
});

describe("guardWebReadPrice", () => {
  test("refuses to sign a Web Read offer above $0.002", async () => {
    const site = fakeWebRead(() => json(200, PAGE), { amount: "3000" });
    const err = await readPage(wrapFetchWithPayment(site.fetchImpl, payingClient()), "https://news.test/a").catch((e) => e);
    expect(String(err)).toContain("exceeds 2000");
    expect(site.seen.some((s) => s.paid)).toBe(false);
  });

  test("refuses a Web Read offer paying another address", async () => {
    const site = fakeWebRead(() => json(200, PAGE), { payTo: "0x000000000000000000000000000000000000dEaD" });
    const err = await readPage(wrapFetchWithPayment(site.fetchImpl, payingClient()), "https://news.test/a").catch((e) => e);
    expect(String(err)).toContain("expected");
    expect(site.seen.some((s) => s.paid)).toBe(false);
  });

  test("leaves other sellers' offers alone", async () => {
    const other = "https://other.example/paid";
    const site = fakeWebRead(() => json(200, { ok: true }), { resource: other, amount: "50000", payTo: "0x000000000000000000000000000000000000dEaD" });
    const res = await wrapFetchWithPayment(site.fetchImpl, payingClient())(other, { method: "POST", body: "{}" });
    expect(res.status).toBe(200);
  });
});

describe("quote and tool", () => {
  test("quote reads the unpaid offer without signing", async () => {
    const site = fakeWebRead(() => json(200, PAGE));
    const q = await quote(site.fetchImpl);
    expect([q.amount, q.network, q.payTo]).toEqual(["2000", "eip155:8453", WEB_READ_PAY_TO]);
    expect(site.seen.every((s) => !s.paid)).toBe(true);
  });

  test("the tool returns a readable, not-charged refusal", async () => {
    const site = fakeWebRead(() => json(422, { error: "too_large", detail: "", charged: false }));
    const out = await runWebReadTool(wrapFetchWithPayment(site.fetchImpl, payingClient()), { url: "https://x.test/big" });
    expect(out).toEqual({ ok: false, error: "too_large", detail: "", retryable: false, charged: false });
  });
});
