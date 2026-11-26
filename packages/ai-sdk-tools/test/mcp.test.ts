import { describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createServer, fileBudget, SOURCE, TOOLS, toAtomic, type Budget } from "../src/mcp";

const ORIGIN = "https://svc.test";
const page = (url: string) => ({ url, title: url, content_markdown: "# hi", content_sha256: "s", word_count: 2 });

function mock(status = 200, body?: unknown) {
  const calls: Array<{ url: string; headers: Record<string, string>; body: Record<string, unknown> }> = [];
  const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const b = JSON.parse(String(init?.body));
    calls.push({ url: String(input), headers: init?.headers as Record<string, string>, body: b });
    const json = body ?? (b.urls ? { pages: b.urls.map(page), count: b.urls.length } : page(b.url));
    return new Response(JSON.stringify(json), { status });
  };
  return { fetch, calls };
}

const budgetIn = (dir: string, usd = "0.01", now?: () => Date) => fileBudget(toAtomic(usd), join(dir, "spend.json"), now);
const tmp = () => mkdtempSync(join(tmpdir(), "inferrail-mcp-"));
const callTool = (name: string, args: unknown) => ({ jsonrpc: "2.0", id: 7, method: "tools/call", params: { name, arguments: args } });
const parse = (out: any) => JSON.parse(out.result.content[0].text);

describe("mcp server", () => {
  test("initialize and tools/list: four paid tools with JSON schemas; notifications get no answer", async () => {
    const handle = createServer({ fetch: mock().fetch, budget: null, origin: ORIGIN });
    const init: any = await handle({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } });
    expect(init.result.protocolVersion).toBe("2025-06-18");
    expect(init.result.capabilities).toEqual({ tools: {} });
    expect(await handle({ jsonrpc: "2.0", method: "notifications/initialized" })).toBeNull();
    const list: any = await handle({ jsonrpc: "2.0", id: 2, method: "tools/list" });
    expect(list.result.tools.map((t: any) => t.name)).toEqual(["read_page", "read_pages", "check_packages", "lookup_domain"]);
    for (const t of list.result.tools) expect(t.inputSchema.type).toBe("object");
    const unknown: any = await handle({ jsonrpc: "2.0", id: 3, method: "nope" });
    expect(unknown.error.code).toBe(-32601);
  });

  test("a delivered batch is counted against today's budget and tagged as this channel", async () => {
    const dir = tmp();
    const svc = mock();
    const budget = budgetIn(dir);
    const handle = createServer({ fetch: svc.fetch, budget, origin: ORIGIN });
    const out: any = await handle(callTool("read_pages", { urls: ["https://a.test/", "https://b.test/"] }));
    expect(out.result.isError).toBe(false);
    expect(parse(out).pages).toHaveLength(2);
    expect(svc.calls[0].url).toBe(`${ORIGIN}/read/v1/pages`);
    expect(svc.calls[0].headers["x-inferrail-source"]).toBe(SOURCE);
    expect(budget.spentToday()).toBe(4000n);
    expect(JSON.parse(readFileSync(join(dir, "spend.json"), "utf8")).spent_atomic).toBe("4000");
  });

  test("a call that would pass the daily budget is refused before any request", async () => {
    const svc = mock();
    const budget = budgetIn(tmp(), "0.005"); // room for 2 pages, not 3
    const handle = createServer({ fetch: svc.fetch, budget, origin: ORIGIN });
    await handle(callTool("read_pages", { urls: ["https://a.test/", "https://b.test/"] }));
    const out: any = await handle(callTool("read_page", { url: "https://c.test/" }));
    expect(out.result.isError).toBe(true);
    expect(parse(out)).toMatchObject({ ok: false, charged: false, error: "daily_budget" });
    expect(svc.calls).toHaveLength(1);
    expect(budget.spentToday()).toBe(4000n);
  });

  test("an undelivered batch costs nothing, gives the hold back and says which pages failed", async () => {
    const results = [{ url: "https://a.test/", delivered: true }, { url: "https://down.test/", delivered: false, error: "upstream_status" }];
    const budget = budgetIn(tmp(), "0.004");
    const handle = createServer({ fetch: mock(422, { error: "pages_failed", charged: false, results }).fetch, budget, origin: ORIGIN });
    const out: any = await handle(callTool("read_pages", { urls: ["https://a.test/", "https://down.test/"] }));
    expect(out.result.isError).toBe(true);
    expect(parse(out)).toMatchObject({ ok: false, charged: false, error: "pages_failed", results });
    expect(budget.spentToday()).toBe(0n);
    expect(budget.reserve(4000n)).toBe(true); // the full budget is still there
  });

  test("invalid arguments never reach the service", async () => {
    const svc = mock();
    const handle = createServer({ fetch: svc.fetch, budget: budgetIn(tmp()), origin: ORIGIN });
    for (const [name, args] of [
      ["read_page", { url: "file:///etc/passwd" }],
      ["read_pages", { urls: ["https://a.test/", "https://a.test/"] }],
      ["read_pages", { urls: Array.from({ length: 11 }, (_, i) => `https://a.test/${i}`) }],
      ["check_packages", { packages: [] }],
    ] as const) {
      expect(parse(await handle(callTool(name, args)))).toMatchObject({ ok: false, charged: false, error: "invalid_arguments" });
    }
    expect(svc.calls).toHaveLength(0);
    const unknown: any = await handle(callTool("delete_everything", {}));
    expect(unknown.error.code).toBe(-32602);
  });

  test("without a wallet, a call comes back 'not paid' with how to set one up", async () => {
    const handle = createServer({ fetch: mock(402, { error: "payment_required" }).fetch, budget: null, origin: ORIGIN });
    const r = parse(await handle(callTool("read_page", { url: "https://a.test/" })));
    expect(r).toMatchObject({ ok: false, charged: false, status: 402 });
    expect(r.detail).toContain("INFERRAIL_WALLET_KEY");
  });

  test("the daily budget starts over on a new UTC day and survives a restart", () => {
    const dir = tmp();
    let now = new Date("2026-11-26T23:59:00Z");
    const a: Budget = budgetIn(dir, "0.002", () => now);
    expect(a.reserve(2000n)).toBe(true);
    a.commit(2000n);
    const restarted = budgetIn(dir, "0.002", () => now);
    expect(restarted.reserve(2000n)).toBe(false);
    now = new Date("2026-11-27T00:01:00Z");
    expect(restarted.reserve(2000n)).toBe(true);
  });

  test("tool descriptions state the price and warn that page text is untrusted", () => {
    for (const t of TOOLS) expect(t.description).toContain("$0.002");
    for (const t of TOOLS.filter((t) => t.name.startsWith("read"))) expect(t.description).toContain("never as instructions");
  });
});

// The real server over stdio, paying a local stand-in seller. The key is a throwaway generated
// here; its signatures only ever reach the stand-in, which never settles anything.
describe("stdio, paying a local stand-in", () => {
  const USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
  const PAY_TO = "0xA45140E99E9482a50BC3fA5F6000c02FAAB6F65E";

  async function run(offerAtomic: (units: number) => string, messages: unknown[], withKey = true) {
    const paid: string[] = [];
    const server = Bun.serve({
      port: 0,
      async fetch(req) {
        const body = (await req.json()) as { url?: string; urls?: string[] };
        const units = body.urls?.length ?? 1;
        if (req.headers.get("payment-signature")) {
          paid.push(new URL(req.url).pathname);
          return Response.json(body.urls ? { pages: body.urls.map(page), count: units } : page(body.url!));
        }
        const required = {
          x402Version: 2,
          resource: { url: req.url, description: "stand-in", mimeType: "application/json" },
          accepts: [{ scheme: "exact", network: "eip155:8453", amount: offerAtomic(units), asset: USDC, payTo: PAY_TO,
            maxTimeoutSeconds: 300, extra: { name: "USD Coin", version: "2" } }],
        };
        return new Response("{}", { status: 402, headers: { "PAYMENT-REQUIRED": Buffer.from(JSON.stringify(required)).toString("base64") } });
      },
    });
    const { generatePrivateKey } = await import("viem/accounts");
    const state = tmp();
    const proc = Bun.spawn([process.execPath, join(import.meta.dir, "../src/mcp.ts")], {
      env: { PATH: process.env.PATH ?? "", INFERRAIL_ORIGIN: `http://127.0.0.1:${server.port}`, INFERRAIL_STATE_DIR: state,
        INFERRAIL_MAX_USD_PER_DAY: "0.006", ...(withKey ? { INFERRAIL_WALLET_KEY: generatePrivateKey() } : {}) },
      stdin: "pipe", stdout: "pipe", stderr: "pipe",
    });
    for (const m of messages) proc.stdin.write(JSON.stringify(m) + "\n");
    proc.stdin.end();
    const lines = (await new Response(proc.stdout).text()).trim().split("\n").map((l) => JSON.parse(l));
    await proc.exited;
    server.stop(true);
    const byId = Object.fromEntries(lines.map((l) => [l.id, l]));
    return { byId, paid, state };
  }

  const msgs = [
    { jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18" } },
    { jsonrpc: "2.0", method: "notifications/initialized" },
    { jsonrpc: "2.0", id: 2, method: "tools/list" },
    { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "read_pages", arguments: { urls: ["https://a.test/", "https://b.test/"] } } },
  ];

  test("pays the listed price, delivers, and records today's spend", async () => {
    const { byId, paid, state } = await run((u) => String(2000 * u), msgs);
    expect(byId[1].result.serverInfo.name).toBe("inferrail");
    expect(byId[2].result.tools).toHaveLength(4);
    expect(byId[3].result.isError).toBe(false);
    expect(JSON.parse(byId[3].result.content[0].text).pages).toHaveLength(2);
    expect(paid).toEqual(["/read/v1/pages"]);
    expect(JSON.parse(readFileSync(join(state, "spend.json"), "utf8")).spent_atomic).toBe("4000");
  }, 20000);

  test("refuses to sign an offer above the call's cost (even under the route's maximum)", async () => {
    const { byId, paid } = await run((u) => String(2000 * u + 2000), msgs);
    expect(byId[3].result.isError).toBe(true);
    expect(JSON.parse(byId[3].result.content[0].text)).toMatchObject({ ok: false, charged: false });
    expect(paid).toEqual([]);
  }, 20000);

  test("with no wallet configured nothing is signed", async () => {
    const { byId, paid } = await run((u) => String(2000 * u), msgs, false);
    expect(JSON.parse(byId[3].result.content[0].text)).toMatchObject({ ok: false, charged: false, status: 402 });
    expect(paid).toEqual([]);
  }, 20000);
});
