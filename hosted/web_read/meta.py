"""Page metadata from the HTML Web Read already fetched, and bot-check page recognition.

`page_metadata(html, final_url)` returns what the page itself declares: description, canonical
URL, authors, published and modified dates, language, site name, image and type. Nothing is
inferred or invented: a field the page doesn't declare (or declares in a form that can't be
validated) is null, or an empty list for authors. When several sources declare the same field,
the first valid one in this order wins:

- description: `<meta name=description>`, `og:description`, `twitter:description`, JSON-LD
- canonical_url: `<link rel=canonical>`, `og:url` (resolved against the page URL; http(s) only)
- authors: `<meta name=author>`, `article:author` (names only, not profile URLs), JSON-LD
- published_at: `article:published_time`, JSON-LD `datePublished`, `<meta name=date|pubdate|...>`
- modified_at: `article:modified_time`, `og:updated_time`, JSON-LD `dateModified`
- language: `<html lang>`, `<meta http-equiv=content-language>`, `og:locale`
- site_name: `og:site_name`, JSON-LD `publisher.name`
- image: `og:image`, `twitter:image` (resolved; http(s) only)
- type: `og:type`, JSON-LD `@type`

Dates are returned as the page wrote them, only if they are ISO 8601 (a date, optionally with a
time). Language tags are returned only if they look like BCP 47.

`bot_challenge(html)` names the vendor when the HTML is a known bot-check interstitial (a page
served instead of the content), recognized only by structures those pages carry and ordinary pages
don't: Amazon's captcha form, Cloudflare's challenge bootstrap, DataDome's captcha frame and
HUMAN's (PerimeterX) block element. A page that merely embeds a captcha widget (a comment or login
form with reCAPTCHA, hCaptcha or Turnstile) is not a challenge.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

MAX_TEXT = 1000
MAX_URL = 2048
MAX_AUTHORS = 10
_ISO = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
_LANG = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")
_DATE_NAMES = ("date", "pubdate", "publishdate", "publish_date", "dc.date.issued",
               "dcterms.created", "dc.date", "sailthru.date", "parsely-pub-date")  # fmt: skip
_ARTICLE_TYPES = ("Article", "NewsArticle", "BlogPosting", "TechArticle", "ScholarlyArticle",
                  "Report", "WebPage", "AboutPage", "FAQPage", "QAPage", "Product",
                  "SoftwareApplication", "Dataset", "Recipe", "Event")  # fmt: skip

FIELDS = ("description", "canonical_url", "authors", "published_at", "modified_at", "language",
          "site_name", "image", "type")  # fmt: skip

# Bot-check interstitials, by vendor: structures only those pages carry.
CHALLENGES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("amazon", re.compile(r"""<form[^>]+action=["'][^"']*/validateCaptcha""", re.I)),
    ("cloudflare", re.compile(
        r"window\._cf_chl_opt\s*=|/cdn-cgi/challenge-platform/h/[bg]/orchestrate")),
    ("datadome", re.compile(
        r"""<(?:iframe|script)[^>]+src=["']https://(?:geo|ct)\.captcha-delivery\.com/""", re.I)),
    ("human", re.compile(r"""<div[^>]+id=["']px-captcha["']""", re.I)),
)  # fmt: skip


def bot_challenge(html: str) -> str | None:
    """The vendor whose bot-check page this is, or None."""
    head = html[:200_000]  # challenge pages are small; the marker is near the top
    for vendor, pattern in CHALLENGES:
        if pattern.search(head):
            return vendor
    return None


def _clean(value: Any, limit: int = MAX_TEXT) -> str | None:
    if not isinstance(value, str):
        return None
    text = re.sub(r"\s+", " ", value).strip()
    return text[:limit] if text else None


def _url(value: Any, base: str) -> str | None:
    text = _clean(value, MAX_URL)
    if not text:
        return None
    url = urljoin(base, text) if base else text
    return url if url.startswith(("http://", "https://")) and len(url) <= MAX_URL else None


def _date(value: Any) -> str | None:
    text = _clean(value, 64)
    return text if text and _ISO.match(text) else None


def _lang(value: Any) -> str | None:
    text = _clean(value, 35)
    if not text:
        return None
    text = text.replace("_", "-")  # og:locale writes en_US
    return text if _LANG.match(text) else None


def _name(value: Any) -> str | None:
    """A person or organization name; a URL is not a name."""
    if isinstance(value, dict):
        value = value.get("name")
    text = _clean(value, 200)
    if not text or re.match(r"^(https?:)?//", text) or "://" in text:
        return None
    return text


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang: str | None = None
        self.meta: list[tuple[str, str]] = []  # (key lowercased, content) in document order
        self.canonical: list[str] = []
        self.jsonld: list[str] = []
        self._in_jsonld = False
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): v or "" for k, v in attrs}
        if tag == "html" and self.lang is None and a.get("lang"):
            self.lang = a["lang"]
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or a.get("itemprop") or "").strip().lower()
            if not key and a.get("http-equiv"):
                key = "http-equiv:" + a["http-equiv"].strip().lower()
            if key and "content" in a:
                self.meta.append((key, a["content"]))
        elif tag == "link" and "canonical" in a.get("rel", "").lower().split():
            self.canonical.append(a.get("href", ""))
        elif tag == "script" and a.get("type", "").strip().lower() == "application/ld+json":
            self._in_jsonld, self._buf = True, []

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_jsonld:
            self._in_jsonld = False
            self.jsonld.append("".join(self._buf))

    def handle_data(self, data: str) -> None:
        if self._in_jsonld:
            self._buf.append(data)


def _jsonld_objects(blocks: list[str]) -> list[dict[str, Any]]:
    """Top-level JSON-LD objects (unwrapping lists and @graph); malformed blocks are skipped."""
    out: list[dict[str, Any]] = []
    for block in blocks[:20]:
        try:
            data = json.loads(block)
        except ValueError:
            continue
        stack = data if isinstance(data, list) else [data]
        for item in stack[:50]:
            if not isinstance(item, dict):
                continue
            graph = item.get("@graph")
            if isinstance(graph, list):
                out.extend(g for g in graph[:50] if isinstance(g, dict))
            else:
                out.append(item)
    return out


def _types(obj: dict[str, Any]) -> list[str]:
    t = obj.get("@type")
    return [x for x in (t if isinstance(t, list) else [t]) if isinstance(x, str)]


def _primary(objects: list[dict[str, Any]]) -> dict[str, Any]:
    """The object describing the page: the first article-like one, else the first one."""
    for obj in objects:
        if any(t in _ARTICLE_TYPES for t in _types(obj)):
            return obj
    return objects[0] if objects else {}


def _first(*values: Any) -> Any:
    return next((v for v in values if v not in (None, "", [])), None)


def page_metadata(html: str, final_url: str = "") -> dict[str, Any]:
    """What the page declares about itself (see the module docstring); absent fields are null."""
    p = _MetaParser()
    try:
        p.feed(html)
        p.close()
    except Exception:  # noqa: BLE001 - metadata never costs the page; malformed HTML gives nulls
        pass
    meta: dict[str, list[str]] = {}
    for key, value in p.meta:
        meta.setdefault(key, []).append(value)

    def m(*keys: str) -> list[str]:
        return [v for k in keys for v in meta.get(k, [])]

    ld = _primary(_jsonld_objects(p.jsonld))
    publisher = ld.get("publisher")
    ld_authors = ld.get("author")
    ld_author_list = ld_authors if isinstance(ld_authors, list) else [ld_authors]

    authors: list[str] = []
    # Authors come from one source only: the first that names anyone (sources often repeat
    # the same people in different spellings).
    for source in (m("author"), m("article:author"), ld_author_list):
        names = [n for n in (_name(r) for r in source) if n]
        if names:
            authors = list(dict.fromkeys(names))[:MAX_AUTHORS]
            break
    ld_type = next(iter(_types(ld)), None)
    return {
        "description": _first(*(_clean(v) for v in m("description")),
                              *(_clean(v) for v in m("og:description")),
                              *(_clean(v) for v in m("twitter:description")),
                              _clean(ld.get("description"))),
        "canonical_url": _first(*(_url(v, final_url) for v in p.canonical),
                                *(_url(v, final_url) for v in m("og:url"))),
        "authors": authors,
        "published_at": _first(*(_date(v) for v in m("article:published_time")),
                               _date(ld.get("datePublished")),
                               *(_date(v) for v in m(*_DATE_NAMES))),
        "modified_at": _first(*(_date(v) for v in m("article:modified_time")),
                              *(_date(v) for v in m("og:updated_time")),
                              _date(ld.get("dateModified"))),
        "language": _first(_lang(p.lang),
                           *(_lang(v) for v in m("http-equiv:content-language")),
                           *(_lang(v) for v in m("og:locale"))),
        "site_name": _first(*(_clean(v, 200) for v in m("og:site_name")),
                            _name(publisher) if isinstance(publisher, dict) else None),
        "image": _first(*(_url(v, final_url) for v in m("og:image", "og:image:url")),
                        *(_url(v, final_url) for v in m("twitter:image"))),
        "type": _first(*(_clean(v, 100) for v in m("og:type")), _clean(ld_type, 100)),
    }  # fmt: skip


def empty_metadata() -> dict[str, Any]:
    return {k: ([] if k == "authors" else None) for k in FIELDS}
