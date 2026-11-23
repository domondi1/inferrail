import { describe, expect, test } from "bun:test";
import {
  call,
  checkOffer,
  guardInferrailPrices,
  inferrailTools,
  ORIGIN,
  PAY_TO,
  ROUTES,
  type Fetch,
} from "../src/index";

type Seen = { url: string; init: RequestInit; body: Record<string, unknown> };

function mockFetch(answers: Array<Response | Error>): { fetch: Fetch; seen: Seen[] } {
  const seen: Seen[] = [];
  const fetch: Fetch = async (input, init) => {
    seen.push({ url: String(input), init: init!, body: JSON.parse(String(init!.body)) });
    const next = answers.shift();
    if (next instanceof Error) throw next;
    return next!;
  };
  return { fetch, seen };
}

const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const opts = { toolCallId: "t", messages: [] } as never;

describe("tools", () => {
  test("every tool posts its route with a request_id and the channel header", async () => {
    const { fetch, seen } = mockFetch(Array.from({ length: 5 }, () => json(200, { done: true })));
    const t = inferrailTools({ fetch });
    await t.readPage.execute!({ url: "https://example.com/" }, opts);
    await t.readPages.execute!({ urls: ["https://a.test/", "https://b.test/"] }, opts);
    await t.checkPackages.execute!(
      { packages: [{ ecosystem: "npm", name: "lodash", version: "4.17.15" }] }, opts);
    await t.lookupDomain.execute!({ domain: "example.com" }, opts);
    await t.findPaidApi.execute!({ need: "read a web page" }, opts);
    expect(seen.map((s) => new URL(s.url).pathname).sort()).toEqual(Object.keys(ROUTES).sort());
    for (const s of seen) {
      expect(s.url.startsWith(ORIGIN)).toBe(true);
      expect(s.init.method).toBe("POST");
      expect((s.init.headers as Record<string, string>)["x-inferrail-source"]).toBe("ai-sdk");
      expect(typeof s.body.request_id).toBe("string");
    }
  });

  test("a delivered result is passed through", async () => {
    const { fetch } = mockFetch([json(200, { title: "Example", word_count: 28 })]);
    const r = await inferrailTools({ fetch }).readPage.execute!({ url: "https://example.com/" }, opts);
    expect(r).toEqual({ ok: true, title: "Example", word_count: 28 });
  });

  test("a network failure is retried once with the same request_id", async () => {
    const { fetch, seen } = mockFetch([new TypeError("socket hang up"), json(200, { ok2: 1 })]);
    const r = await call(fetch, "/read/v1/page", { url: "https://example.com/" });
    expect(r.ok).toBe(true);
    expect(seen.length).toBe(2);
    expect(seen[0].body.request_id).toBe(seen[1].body.request_id);
  });

  test("refusals and an unpaid fetch come back uncharged, with a reason the model can use", async () => {
    const { fetch } = mockFetch([
      json(422, { error: "robots_disallowed", detail: "robots.txt", charged: false }),
      json(402, {}),
      new Response("bad gateway", { status: 502 }),
    ]);
    expect(await call(fetch, "/read/v1/page", { url: "https://x.test/" })).toEqual({
      ok: false, charged: false, status: 422, error: "robots_disallowed", detail: "robots.txt" });
    const unpaid = await call(fetch, "/read/v1/page", { url: "https://x.test/" });
    expect(unpaid).toMatchObject({ ok: false, charged: false, status: 402, error: "payment_required" });
    expect((unpaid as { detail: string }).detail).toContain("@x402/fetch");
    expect(await call(fetch, "/read/v1/page", { url: "https://x.test/" })).toEqual({
      ok: false, charged: false, status: 502, error: "http_502" });
  });

  test("input schemas match the service limits", () => {
    const t = inferrailTools({ fetch: mockFetch([]).fetch });
    const schema = (x: { inputSchema: unknown }) => x.inputSchema as { safeParse(v: unknown): { success: boolean } };
    expect(schema(t.readPages).safeParse({ urls: Array(11).fill("https://a.test/") }).success).toBe(false);
    expect(schema(t.checkPackages).safeParse({ packages: [] }).success).toBe(false);
    expect(schema(t.checkPackages).safeParse({
      packages: [{ ecosystem: "Debian", name: "x", version: "1" }] }).success).toBe(false);
    expect(schema(t.lookupDomain).safeParse({ domain: "example.com", records: ["MX"] }).success).toBe(true);
    expect(schema(t.findPaidApi).safeParse({ need: "x".repeat(301) }).success).toBe(false);
  });
});

describe("price guard", () => {
  const ok = { amount: "2000", payTo: PAY_TO.read };

  test("accepts the listed price and address, and ignores other sellers", () => {
    expect(checkOffer(`${ORIGIN}/read/v1/page`, ok)).toBeUndefined();
    expect(checkOffer(`${ORIGIN}/read/v1/pages`, { amount: "20000", payTo: PAY_TO.read })).toBeUndefined();
    expect(checkOffer(`${ORIGIN}/pkg/v1/check`, { amount: "2000", payTo: PAY_TO.packages })).toBeUndefined();
    expect(checkOffer("https://other.example/api", { amount: "99999999", payTo: "0x1" })).toBeUndefined();
  });

  test("refuses a higher price, another address or an unknown Inferrail route", () => {
    expect(checkOffer(`${ORIGIN}/read/v1/page`, { amount: "2001", payTo: PAY_TO.read })).toMatchObject({ abort: true });
    expect(checkOffer(`${ORIGIN}/read/v1/pages`, { amount: "20001", payTo: PAY_TO.read })).toMatchObject({ abort: true });
    expect(checkOffer(`${ORIGIN}/read/v1/page`, { amount: "2000", payTo: PAY_TO.find })).toMatchObject({ abort: true });
    expect(checkOffer(`${ORIGIN}/new/v1/thing`, ok)).toMatchObject({ abort: true });
    expect(checkOffer(`${ORIGIN}/read/v1/page`, { amount: "lots", payTo: PAY_TO.read })).toMatchObject({ abort: true });
  });

  test("installs itself as the client's before-payment hook", async () => {
    let hook: ((ctx: never) => Promise<unknown>) | undefined;
    const client = { onBeforePaymentCreation(h: (ctx: never) => Promise<unknown>) { hook = h; } };
    expect(guardInferrailPrices(client)).toBe(client);
    const verdict = await hook!({ paymentRequired: { resource: { url: `${ORIGIN}/read/v1/page` } },
      selectedRequirements: { amount: "5000", payTo: PAY_TO.read } } as never);
    expect(verdict).toMatchObject({ abort: true });
  });
});

// LIVE=1 bun test: unpaid requests only (each answers 402 and costs nothing). Checks the live
// offers against the guard, so a price or address change on the service fails here first.
describe.skipIf(process.env.LIVE !== "1")("live offers (unpaid)", () => {
  const bodies: Record<string, unknown> = {
    "/read/v1/page": { url: "https://example.com/" },
    "/read/v1/pages": { urls: Array.from({ length: 10 }, (_, i) => `https://example.com/${i}`) },
    "/find/v1/find": { need: "read a web page" },
    "/pkg/v1/check": { packages: [{ ecosystem: "npm", name: "lodash", version: "4.17.15" }] },
    "/domain/v1/lookup": { domain: "example.com" },
  };
  for (const [path, body] of Object.entries(bodies)) {
    test(`${path} offer passes the guard`, async () => {
      const r = await fetch(`${ORIGIN}${path}`, { method: "POST", body: JSON.stringify(body),
        headers: { "content-type": "application/json" } });
      expect(r.status).toBe(402);
      const required = JSON.parse(atob(r.headers.get("payment-required")!));
      const base = required.accepts.find((a: { network: string }) => a.network === "eip155:8453");
      expect(required.resource.url).toBe(`${ORIGIN}${path}`);
      expect(checkOffer(required.resource.url, base)).toBeUndefined();
    }, 30_000);
  }
});
