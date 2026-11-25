# Web Read: what your model receives per page

An agent that reads a page with a plain HTTP fetch hands its model the page's raw HTML. Web
Read returns the readable content as Markdown, plus the page's own metadata. This benchmark
measures the difference in tokens, page by page. Nothing is paid: it runs Web Read's
production extraction locally, next to a plain GET of the same URL.

```bash
python3 benchmarks/web_read/tokens.py            # the 20 pages below
python3 benchmarks/web_read/tokens.py urls.txt   # your own pages, one URL per line
```

## Results (2026-11-25, production User-Agent)

Tokens are estimated as characters ÷ 4.

| Page | Web Read | Raw HTML | Ratio | Web Read metadata |
|---|---|---|---|---|
| https://docs.python.org/3/whatsnew/3.13.html | 53,337 | 127,486 | 2x | canonical_url, description, language |
| https://go.dev/doc/devel/release | 21,607 | 37,671 | 2x | canonical_url, language |
| https://kubernetes.io/releases/ | 1,752 | 13,052 | 7x | canonical_url, description, language |
| https://www.postgresql.org/docs/release/17.0/ | 15,255 | 33,580 | 2x | language |
| https://react.dev/blog | 2,514 | 21,801 | 9x | - |
| https://blog.rust-lang.org/ | 10,438 | 24,837 | 2x | description, language |
| https://nodejs.org/en/blog/release/v22.11.0 | 2,149 | 69,642 | 32x | canonical_url, description, language |
| https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/402 | 930 | 52,235 | 56x | canonical_url, description, language |
| https://docs.stripe.com/changelog | refused, not charged: too_large | 976,114 | - | - |
| https://aws.amazon.com/about-aws/whats-new/ | 154 | 83,021 | 539x | canonical_url, description, language |
| https://pypi.org/project/requests/ | 13,388 | 83,530 | 6x | canonical_url, description, language |
| https://nvd.nist.gov/vuln/detail/CVE-2024-3094 | refused, not charged: no_readable_content | 567 | - | - |
| https://github.com/advisories/GHSA-9wx4-h78v-vm56 | 922 | 35,707 | 39x | canonical_url, description, language |
| https://arxiv.org/abs/1706.03762 | 1,731 | 10,767 | 6x | canonical_url, description, language |
| https://en.wikipedia.org/wiki/Large_language_model | 41,056 | 266,579 | 6x | published_at, authors, canonical_url, language |
| https://www.bbc.com/news/technology | 3,012 | 142,576 | 47x | canonical_url, description, language |
| https://techcrunch.com/ | 6,481 | 106,197 | 16x | published_at, canonical_url, description, language |
| https://x402.org/ | 974 | 22,563 | 23x | canonical_url, description, language |
| https://docs.cdp.coinbase.com/x402/welcome | 447 | 113,480 | 254x | canonical_url, language |
| https://www.npmjs.com/package/zod | refused, not charged: upstream_status | 1,393 | - | - |

## Reading it honestly

- **17 of 20 delivered.**
  - Across those pages, the median is about **9× fewer tokens** than the raw HTML.
  - It's about 2× on long documentation pages (Python, Go, PostgreSQL), where most of the HTML
    is content.
  - It's 30–56× on pages heavy with markup and navigation (Node.js releases, MDN, BBC, GitHub
    advisories).
- **What that costs.** At an LLM input price of $3 per million tokens:
  - a 100,000-token raw page costs $0.30 of context;
  - the same page through Web Read costs $0.002, plus the much smaller Markdown.
- **Thin extraction is a miss, not a win.** AWS "What's New" builds its list in JavaScript.
  Web Read returned 154 tokens of it (a 539× ratio), which isn't the list. Don't count it.
- **Refused, and not charged:**
  - pages with no text until JavaScript runs (NVD);
  - bot walls (npm);
  - pages over 2 MB (Stripe's changelog, 3.9 MB).
  - Web Read doesn't run JavaScript or get around bot checks. A browser-based reader will read
    some of these pages.
- **Metadata.** Most pages declare a canonical URL, a description and a language, and Web
  Read returns them as JSON fields. News and encyclopedia pages also declare a published date
  and authors. Raw HTML has the same facts buried in markup.
