import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { call, CREDITS_PAGE_PATH, creditsFetch } from "../src/core";
import { inferrailCreditTools } from "../src/index";

const ORIGIN = "https://svc.test";
const KEY = "irw_test-key";

// A stand-in for Web Read's credits route: 2 credits per delivered page, a balance, no-key 401.
function service(balance = 10, broken: string[] = []) {
  const seen: Array<{ url: string; auth: string | null; body: Record<string, unknown> }> = [];
  const fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = new Headers(init?.headers);
    const body = JSON.parse(String(init?.body ?? "{}"));
    seen.push({ url: String(input), auth: headers.get("authorization"), body });
    if (headers.get("authorization") !== `Bearer ${KEY}`) {
      return Response.json({ error: "workspace_key_required", charged: false }, { status: 401 });
    }
    if (broken.includes(body.url)) {
      return Response.json({ error: "upstream_status", charged: false }, { status: 422 });
    }
    if (balance < 2) {
      return Response.json(
        {
          error: "insufficient_credits",
          charged: false,
          credits_needed: 2,
          credits_remaining: balance,
          top_up: { card: "POST https://svc.test/hosted/v1/credits/checkout" },
        },
        { status: 402 },
      );
    }
    balance -= 2;
    return Response.json({
      url: body.url,
      title: "T",
      content_markdown: "# T",
      content_sha256: "s",
      receipt: { rail: "credits", credits_charged: 2, credits_remaining: balance },
    });
  };
  return { fetch, seen, balance: () => balance };
}

describe("credits fetch", () => {
  test("a page read goes to the credits route with the key, and is charged 2 credits", async () => {
    const svc = service();
    const r = await call(creditsFetch(KEY, svc.fetch, ORIGIN), "/read/v1/page", { url: "https://a.test/" }, ORIGIN);
    expect(r.ok).toBe(true);
    expect(svc.seen[0].url).toBe(`${ORIGIN}${CREDITS_PAGE_PATH}`);
    expect(svc.seen[0].auth).toBe(`Bearer ${KEY}`);
    expect(svc.balance()).toBe(8);
  });

  test("a batch is read page by page: delivered pages are charged, failures listed", async () => {
    const svc = service(10, ["https://down.test/"]);
    const r: any = await call(
      creditsFetch(KEY, svc.fetch, ORIGIN),
      "/read/v1/pages",
      { urls: ["https://a.test/", "https://down.test/", "https://c.test/"] },
      ORIGIN,
    );
    expect(r.ok).toBe(true);
    expect(r.pages.map((p: any) => p.url)).toEqual(["https://a.test/", "https://c.test/"]);
    expect(r.failed).toEqual([{ url: "https://down.test/", error: "upstream_status" }]);
    expect(r.charged_pages).toBe(2);
    expect(svc.balance()).toBe(6);
  });

  test("too few credits says how a person tops up, and nothing is charged", async () => {
    const svc = service(1);
    const r: any = await call(creditsFetch(KEY, svc.fetch, ORIGIN), "/read/v1/page", { url: "https://a.test/" }, ORIGIN);
    expect(r).toMatchObject({ ok: false, charged: false, status: 402, error: "insufficient_credits" });
    expect(r.detail).toContain("/hosted/v1/credits/checkout");
  });

  test("a batch refused as a whole (bad key) says so, instead of a list of failures", async () => {
    const svc = service();
    const r: any = await call(creditsFetch("irw_wrong", svc.fetch, ORIGIN), "/read/v1/pages",
      { urls: ["https://a.test/", "https://b.test/"] }, ORIGIN);
    expect(r).toMatchObject({ ok: false, charged: false, status: 401, error: "workspace_key_required" });
  });

  test("a batch where every page fails reports pages_failed with each page's reason", async () => {
    const svc = service(10, ["https://down.test/", "https://gone.test/"]);
    const r: any = await call(creditsFetch(KEY, svc.fetch, ORIGIN), "/read/v1/pages",
      { urls: ["https://down.test/", "https://gone.test/"] }, ORIGIN);
    expect(r).toMatchObject({ ok: false, charged: false, status: 422, error: "pages_failed" });
    expect(r.results).toEqual([
      { url: "https://down.test/", delivered: false, error: "upstream_status" },
      { url: "https://gone.test/", delivered: false, error: "upstream_status" },
    ]);
    expect(svc.balance()).toBe(10);
  });

  test("a key that isn't an Inferrail workspace key is refused up front", () => {
    expect(() => creditsFetch("sk_live_nope")).toThrow();
  });

  test("the AI SDK credit tools read pages from credits", async () => {
    const svc = service();
    const tools = inferrailCreditTools({ apiKey: KEY, fetch: svc.fetch, origin: ORIGIN });
    expect(Object.keys(tools).sort()).toEqual(["readPage", "readPages"]);
    const r: any = await tools.readPage.execute!({ url: "https://a.test/" }, { toolCallId: "t", messages: [] });
    expect(r.ok).toBe(true);
    expect(svc.balance()).toBe(8);
  });
});

describe("mcp server with INFERRAIL_API_KEY (stdio, local stand-in)", () => {
  test("offers the two reader tools and pays a page from credits, under the daily budget", async () => {
    let balance = 10;
    const server = Bun.serve({
      port: 0,
      async fetch(req) {
        const url = new URL(req.url);
        if (url.pathname !== CREDITS_PAGE_PATH) return new Response("no", { status: 404 });
        if (req.headers.get("authorization") !== `Bearer ${KEY}`) {
          return Response.json({ error: "workspace_key_required" }, { status: 401 });
        }
        const body = (await req.json()) as { url: string };
        balance -= 2;
        return Response.json({ url: body.url, title: "T", content_markdown: "# T", content_sha256: "s" });
      },
    });
    const proc = Bun.spawn([process.execPath, join(import.meta.dir, "../src/mcp.ts")], {
      env: {
        PATH: process.env.PATH ?? "",
        INFERRAIL_ORIGIN: `http://127.0.0.1:${server.port}`,
        INFERRAIL_API_KEY: KEY,
        INFERRAIL_MAX_USD_PER_DAY: "0.002",
        INFERRAIL_STATE_DIR: mkdtempSync(join(tmpdir(), "inferrail-credits-")),
      },
      stdin: "pipe",
      stdout: "pipe",
      stderr: "pipe",
    });
    const msgs = [
      { jsonrpc: "2.0", id: 1, method: "tools/list" },
      { jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: "read_page", arguments: { url: "https://a.test/" } } },
      { jsonrpc: "2.0", id: 3, method: "tools/call", params: { name: "read_page", arguments: { url: "https://b.test/" } } },
      { jsonrpc: "2.0", id: 4, method: "tools/call", params: { name: "check_packages", arguments: { packages: [] } } },
    ];
    for (const m of msgs) proc.stdin.write(JSON.stringify(m) + "\n");
    proc.stdin.end();
    const lines = (await new Response(proc.stdout).text())
      .trim()
      .split("\n")
      .map((l) => JSON.parse(l));
    await proc.exited;
    server.stop(true);
    const byId = Object.fromEntries(lines.map((l) => [l.id, l]));
    expect(byId[1].result.tools.map((t: any) => t.name)).toEqual(["read_page", "read_pages"]);
    expect(byId[1].result.tools[0].description).toContain("prepaid Inferrail credits");
    expect(byId[2].result.isError).toBe(false);
    // The $0.002 daily budget covers one page: the second is refused before any request.
    expect(JSON.parse(byId[3].result.content[0].text).error).toBe("daily_budget");
    expect(balance).toBe(8);
    expect(byId[4].error.code).toBe(-32602); // no credits route for other services in this mode
  }, 20000);
});
