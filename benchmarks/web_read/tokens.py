"""How much text does an agent's model receive per page: Web Read's markdown vs the raw HTML a
plain HTTP fetch returns? Runs Web Read's production extraction locally (nothing is paid, no
service is called) next to a plain GET of the same URL, and prints a table.

    python3 benchmarks/web_read/tokens.py [urls.txt]

Tokens are estimated as characters / 4. Results change as the pages change.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hosted" / "web_read"))
SERVICE = ROOT / "hosted" / "web_read" / "service.py"
spec = importlib.util.spec_from_file_location("web_read_service", SERVICE)
assert spec is not None and spec.loader is not None
svc = importlib.util.module_from_spec(spec)
sys.modules["web_read_service"] = svc
spec.loader.exec_module(svc)

URLS = [
    "https://docs.python.org/3/whatsnew/3.13.html", "https://go.dev/doc/devel/release",
    "https://kubernetes.io/releases/", "https://www.postgresql.org/docs/release/17.0/",
    "https://react.dev/blog", "https://blog.rust-lang.org/",
    "https://nodejs.org/en/blog/release/v22.11.0",
    "https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/402",
    "https://docs.stripe.com/changelog", "https://aws.amazon.com/about-aws/whats-new/",
    "https://pypi.org/project/requests/", "https://nvd.nist.gov/vuln/detail/CVE-2024-3094",
    "https://github.com/advisories/GHSA-9wx4-h78v-vm56", "https://arxiv.org/abs/1706.03762",
    "https://en.wikipedia.org/wiki/Large_language_model", "https://www.bbc.com/news/technology",
    "https://techcrunch.com/", "https://x402.org/", "https://docs.cdp.coinbase.com/x402/welcome",
    "https://www.npmjs.com/package/zod",
]  # fmt: skip


async def main(urls: list[str]) -> None:
    import dataclasses

    ua = next(f for f in dataclasses.fields(svc.Settings) if f.name == "user_agent").default
    task = svc.page_task(svc.fetch.Fetcher(ua))
    print("| Page | Web Read | Raw HTML | Ratio | Web Read metadata |")
    print("|---|---|---|---|---|")
    headers = {"user-agent": ua}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as c:
        for url in urls:
            status, row = await task.deliver(task.request_model(url=url), lambda *a: None)
            try:
                raw = len((await c.get(url)).text) // 4
            except httpx.HTTPError:
                raw = None
            if status == 200:
                tokens = len(row["content_markdown"]) // 4
                meta = [k for k in ("published_at", "authors", "canonical_url", "description",
                                    "language") if (row.get("metadata") or {}).get(k)]  # fmt: skip
                ratio = f"{raw / max(tokens, 1):.0f}x" if raw else "-"
                print(f"| {url} | {tokens:,} | {raw:,} | {ratio} | {', '.join(meta) or '-'} |")
            else:
                print(f"| {url} | refused, not charged: {row['error']} | "
                      f"{f'{raw:,}' if raw else '-'} | - | - |")  # fmt: skip
            time.sleep(0.5)


if __name__ == "__main__":
    lines = Path(sys.argv[1]).read_text().split() if len(sys.argv) > 1 else URLS
    asyncio.run(main(lines))
