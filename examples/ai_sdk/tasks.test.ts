import { describe, expect, test } from "bun:test";
import { MockLanguageModelV3 } from "ai/test";
import { reviewDependencies, researchBrief, vetLink } from "./tasks";
import { capSpending, paidFetchFromEnv, toAtomic, type Hookable } from "./wallet";

const usage = {
  inputTokens: { total: 1, noCache: 1, cacheRead: 0, cacheWrite: 0 },
  outputTokens: { total: 1, text: 1, reasoning: 0 },
};

/** A model that makes the given tool calls in turn, then answers; records the tools offered. */
function scripted(calls: Array<{ tool: string; input: unknown }>) {
  const offered: string[][] = [];
  let i = 0;
  const model = new MockLanguageModelV3({
    doGenerate: async (opts) => {
      offered.push((opts.tools ?? []).map((t) => t.name).sort());
      const call = calls[i++];
      if (call) {
        return { content: [{ type: "tool-call", toolCallId: `c${i}`, toolName: call.tool,
          input: JSON.stringify(call.input) }], finishReason: { unified: "tool-calls", raw: undefined },
          usage, warnings: [] };
      }
      return { content: [{ type: "text", text: "report" }], finishReason: { unified: "stop", raw: undefined },
        usage, warnings: [] };
    },
  });
  return { model, offered };
}

/** The service, answering each route with its documented shape. */
function service(answers: Record<string, unknown>) {
  const hits: Array<{ path: string; body: Record<string, unknown> }> = [];
  const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = new URL(String(input)).pathname;
    hits.push({ path, body: JSON.parse(String(init?.body)) });
    return new Response(JSON.stringify(answers[path] ?? {}), { status: path in answers ? 200 : 404 });
  };
  return { fetch, hits };
}

describe("tasks", () => {
  test("reviewDependencies offers only checkPackages and sends every package in one check", async () => {
    const { model, offered } = scripted([{ tool: "checkPackages", input: { packages: [
      { ecosystem: "npm", name: "lodash", version: "4.17.15" },
      { ecosystem: "npm", name: "express", version: "4.18.2" }] } }]);
    const svc = service({ "/pkg/v1/check": { results: [{ name: "lodash", status: "vulnerable" }] } });
    const r = await reviewDependencies({ model, fetch: svc.fetch }, { lodash: "4.17.15", express: "4.18.2" });
    expect(offered[0]).toEqual(["checkPackages"]);
    expect(svc.hits.map((h) => h.path)).toEqual(["/pkg/v1/check"]);
    expect((svc.hits[0].body.packages as unknown[]).length).toBe(2);
    expect(r.steps[0].toolResults[0].output).toMatchObject({ ok: true });
    expect(r.text).toBe("report");
  });

  test("researchBrief reads all sources in one batch call", async () => {
    const urls = ["https://a.test/1", "https://b.test/2", "https://c.test/3"];
    const { model, offered } = scripted([{ tool: "readPages", input: { urls } }]);
    const svc = service({ "/read/v1/pages": { pages: urls.map((url) => ({ url, title: "t" })) } });
    await researchBrief({ model, fetch: svc.fetch }, "What changed?", urls);
    expect(offered[0]).toEqual(["readPages"]);
    expect(svc.hits).toHaveLength(1);
    expect(svc.hits[0].body.urls).toEqual(urls);
  });

  test("vetLink looks up the domain and reads the page", async () => {
    const { model, offered } = scripted([
      { tool: "lookupDomain", input: { domain: "example.com" } },
      { tool: "readPage", input: { url: "https://example.com/" } },
    ]);
    const svc = service({ "/domain/v1/lookup": { domain: "example.com" }, "/read/v1/page": { title: "Example" } });
    await vetLink({ model, fetch: svc.fetch }, "https://example.com/");
    expect(offered[0]).toEqual(["lookupDomain", "readPage"]);
    expect(svc.hits.map((h) => h.path)).toEqual(["/domain/v1/lookup", "/read/v1/page"]);
  });

  test("a refused read reaches the model as an uncharged result, not an exception", async () => {
    const { model } = scripted([{ tool: "readPages", input: { urls: ["https://a.test/x"] } }]);
    const fetch = async () => new Response(JSON.stringify({ error: "robots_disallowed", charged: false }), { status: 422 });
    const r = await researchBrief({ model, fetch }, "q", ["https://a.test/x"]);
    expect(r.steps[0].toolResults[0].output).toEqual({ ok: false, charged: false, status: 422, error: "robots_disallowed" });
  });
});

describe("spending", () => {
  function fakeClient() {
    let hook: Parameters<Hookable["onBeforePaymentCreation"]>[0] | undefined;
    const client: Hookable = { onBeforePaymentCreation: (h) => { hook = h; } };
    const pay = (amount: string) => hook!({ paymentRequired: {}, selectedRequirements: { amount, payTo: "0x1" } });
    return { client, pay };
  }

  test("the cap allows payments up to MAX_USD for the run and refuses the next one", async () => {
    const { client, pay } = fakeClient();
    const spent = capSpending(client, "0.005");
    expect(await pay("2000")).toBeUndefined();
    expect(await pay("2000")).toBeUndefined();
    expect(await pay("2000")).toMatchObject({ abort: true });
    expect(spent()).toBe(4000n);
  });

  test("dollar amounts convert exactly", () => {
    expect(toAtomic("0.01")).toBe(10000n);
    expect(toAtomic("1")).toBe(1000000n);
    expect(() => toAtomic("0.0000001")).toThrow();
    expect(() => toAtomic("-1")).toThrow();
  });

  test("no key, no paying fetch: the examples never create a wallet", () => {
    expect(() => paidFetchFromEnv({})).toThrow(/never create or fund a wallet/);
    expect(() => paidFetchFromEnv({ EVM_PRIVATE_KEY: "0x1234" })).toThrow();
  });

  test("with a key, the fetch is built with the default 0.01 USD cap", () => {
    const r = paidFetchFromEnv({ EVM_PRIVATE_KEY: "0x" + "11".repeat(32) });
    expect(r.maxUsd).toBe("0.01");
    expect(typeof r.fetch).toBe("function");
    expect(r.spent()).toBe(0n);
  });
});
