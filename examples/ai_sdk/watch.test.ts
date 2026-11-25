import { describe, expect, test } from "bun:test";
import { checkForChanges, listChanges } from "./watch";

const page = (url: string, v: string) => ({ url, title: url, content_markdown: `# ${v}`, content_sha256: `sha-${url}-${v}` });

function service(version: Record<string, string>, fail: string[] = []) {
  const calls: string[][] = [];
  const fetch = async (_: RequestInfo | URL, init?: RequestInit) => {
    const { urls } = JSON.parse(String(init?.body)) as { urls: string[] };
    calls.push(urls);
    const bad = urls.filter((u) => fail.includes(u));
    if (bad.length) {
      return new Response(JSON.stringify({ error: "pages_failed", charged: false,
        results: urls.map((u) => ({ url: u, delivered: !bad.includes(u), ...(bad.includes(u) ? { error: "upstream_status" } : {}) })) }), { status: 422 });
    }
    return new Response(JSON.stringify({ pages: urls.map((u) => page(u, version[u] ?? "v1")), count: urls.length }), { status: 200 });
  };
  return { fetch, calls };
}

describe("watch", () => {
  test("first check: every page is new; second check: only the changed page comes back", async () => {
    const urls = ["https://a.test/changelog", "https://b.test/releases"];
    const first = await checkForChanges(service({}).fetch, urls);
    expect(first.changed.map((p) => p.url)).toEqual(urls);
    expect(first.paidPages).toBe(2);
    const second = await checkForChanges(service({ "https://b.test/releases": "v2" }).fetch, urls, first.state);
    expect(second.changed.map((p) => p.url)).toEqual(["https://b.test/releases"]);
    expect(second.unchanged).toEqual(["https://a.test/changelog"]);
    expect(second.state["https://b.test/releases"].sha256).toBe("sha-https://b.test/releases-v2");
  });

  test("a failed page: the batch is resent without it (the failed batch cost nothing) and it is reported", async () => {
    const svc = service({}, ["https://down.test/x"]);
    const r = await checkForChanges(svc.fetch, ["https://a.test/1", "https://down.test/x", "https://c.test/3"]);
    expect(svc.calls).toEqual([["https://a.test/1", "https://down.test/x", "https://c.test/3"], ["https://a.test/1", "https://c.test/3"]]);
    expect(r.failed).toEqual([{ url: "https://down.test/x", error: "upstream_status" }]);
    expect(r.paidPages).toBe(2);
    expect(r.state["https://down.test/x"]).toBeUndefined();
  });

  test("more than 10 URLs go out in paid batches of 10; duplicates are read once", async () => {
    const svc = service({});
    const urls = Array.from({ length: 23 }, (_, i) => `https://docs.test/${i}`);
    const r = await checkForChanges(svc.fetch, [...urls, urls[0]]);
    expect(svc.calls.map((c) => c.length)).toEqual([10, 10, 3]);
    expect(r.paidPages).toBe(23);
  });

  test("without a model, the report lists changed pages (first check vs changed) and unread ones", async () => {
    const urls = ["https://a.test/changelog", "https://b.test/releases"];
    const first = await checkForChanges(service({}).fetch, urls);
    const svc = service({ "https://b.test/releases": "v2" }, ["https://down.test/x"]);
    const check = await checkForChanges(svc.fetch, [...urls, "https://c.test/new", "https://down.test/x"], first.state);
    const report = listChanges(check, first.state);
    expect(report.split("\n")).toEqual([
      "**2 changed**, 1 unchanged, 1 not read (not charged). Paid for 3 page(s).",
      `- [https://b.test/releases](https://b.test/releases): changed since ${first.state["https://b.test/releases"].checkedAt}`,
      "- [https://c.test/new](https://c.test/new): first check",
      "- not read: https://down.test/x (upstream_status)",
    ]);
  });
});
