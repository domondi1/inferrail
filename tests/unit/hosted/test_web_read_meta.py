"""Web Read page metadata and bot-check refusal. Proves: metadata is what the page declares and
nothing else (absent -> null / []), source precedence, malformed values are dropped rather than
passed through, URLs resolve and must be http(s); bot-check interstitials from known vendors are
refused and never charged while ordinary pages that embed a captcha widget are delivered; a
genuinely short page is still a paid page; the delivered page carries the metadata."""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

pytest.importorskip("x402")
pytest.importorskip("eth_account")

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _x402_fake_facilitator import FakeFacilitator  # noqa: E402
from test_web_read import _client, _paid, svc  # noqa: E402

meta = svc._sibling("meta")
PAY_TO = "0x" + "6" * 40
HTML = {"content-type": "text/html; charset=utf-8"}
ROBOTS_OK = httpx.Response(200, text="User-agent: *\nAllow: /\n")

ARTICLE = """<!doctype html><html lang="en-GB"><head>
<title>Paying per call</title>
<meta name="description" content="  How agents pay
  per call.  ">
<meta property="og:description" content="OG description (ignored: meta description wins)">
<link rel="canonical" href="/articles/paying-per-call">
<meta property="og:url" content="https://other.test/ignored">
<meta name="author" content="Ada Lovelace">
<meta property="article:author" content="https://news.test/profile/ada">
<meta property="article:published_time" content="2026-11-20T09:30:00Z">
<meta property="article:modified_time" content="2026-11-21">
<meta property="og:site_name" content="News Test">
<meta property="og:image" content="/img/cover.png">
<meta property="og:type" content="article">
<script type="application/ld+json">{"@type": "NewsArticle", "datePublished": "1999-01-01",
 "author": [{"@type": "Person", "name": "Someone Else"}]}</script>
</head><body><article><h1>Paying per call</h1><p>Agents pay per call for pages.</p></article>
</body></html>"""


def test_metadata_is_what_the_page_declares_with_documented_precedence():
    md = meta.page_metadata(ARTICLE, "https://news.test/a?x=1")
    assert md == {
        "description": "How agents pay per call.",
        "canonical_url": "https://news.test/articles/paying-per-call",
        "authors": ["Ada Lovelace"],  # meta author beats JSON-LD; the profile URL isn't a name
        "published_at": "2026-11-20T09:30:00Z",  # article:published_time beats JSON-LD
        "modified_at": "2026-11-21",
        "language": "en-GB",
        "site_name": "News Test",
        "image": "https://news.test/img/cover.png",
        "type": "article",
    }


def test_absent_metadata_is_null_never_invented():
    md = meta.page_metadata("<html><head><title>T</title></head><body><p>x</p></body></html>",
                            "https://news.test/")  # fmt: skip
    assert md == meta.empty_metadata()
    assert md["authors"] == [] and all(v is None for k, v in md.items() if k != "authors")


def test_json_ld_fills_what_meta_tags_do_not():
    page = """<html><head><script type="application/ld+json">
    {"@context": "https://schema.org", "@graph": [
      {"@type": "WebSite", "name": "Site"},
      {"@type": "BlogPosting", "datePublished": "2026-01-02T03:04:05+01:00",
       "dateModified": "2026-01-03", "description": "From JSON-LD",
       "author": [{"@type": "Person", "name": "Grace Hopper"}, {"name": "Alan Turing"},
                  "https://example.test/not-a-name"],
       "publisher": {"@type": "Organization", "name": "Blog Inc"}}]}
    </script></head><body><p>x</p></body></html>"""
    md = meta.page_metadata(page, "https://blog.test/p")
    assert md["published_at"] == "2026-01-02T03:04:05+01:00"
    assert md["modified_at"] == "2026-01-03"
    assert md["authors"] == ["Grace Hopper", "Alan Turing"]
    assert md["description"] == "From JSON-LD" and md["site_name"] == "Blog Inc"
    assert md["type"] == "BlogPosting"


@pytest.mark.parametrize(
    ("head", "field", "expected"),
    [
        ('<meta property="article:published_time" content="last Tuesday">', "published_at", None),
        ('<meta property="article:published_time" content="20/11/2026">', "published_at", None),
        ('<meta name="date" content="2026-11-20">', "published_at", "2026-11-20"),
        ('<link rel="canonical" href="javascript:alert(1)">', "canonical_url", None),
        ('<link rel="canonical" href="ftp://x.test/a">', "canonical_url", None),
        ('<meta property="og:image" content="data:image/png;base64,AAAA">', "image", None),
        ('<meta property="og:locale" content="en_US">', "language", "en-US"),
        ('<meta property="og:locale" content="not a language!">', "language", None),
        ('<meta name="author" content="https://x.test/u/1">', "authors", []),
        ('<meta name="description" content="   ">', "description", None),
        ('<script type="application/ld+json">{not json</script>', "published_at", None),
    ],
)  # fmt: skip
def test_malformed_values_are_dropped_not_passed_through(head, field, expected):
    md = meta.page_metadata(f"<html><head>{head}</head><body><p>x</p></body></html>",
                            "https://news.test/")  # fmt: skip
    assert md[field] == expected


def test_long_values_are_capped_and_whitespace_collapsed():
    long = "word " * 1000
    md = meta.page_metadata(f'<meta name="description" content="{long}">', "https://n.test/")
    assert md["description"] is not None and len(md["description"]) <= meta.MAX_TEXT
    assert "  " not in md["description"]


def test_broken_html_never_raises():
    md = meta.page_metadata("<html lang=en><meta name=description content='a'><<</<>>&#xZZZ;",
                            "https://n.test/")  # fmt: skip
    assert md["language"] == "en" and md["description"] == "a"


# -- bot checks --------------------------------------------------------------------------------
AMAZON = """<html lang="en-us"><head><title>Amazon.com</title></head><body>
<h4>Click the button below to continue shopping</h4>
<form method="get" action="/errors/validateCaptcha" name="">
<button type="submit">Continue shopping</button></form>
<a href="/gp/help/customer/display.html">Conditions of Use</a></body></html>"""
CLOUDFLARE = """<html><head><title>Just a moment...</title></head><body>
<noscript>Enable JavaScript and cookies to continue</noscript>
<script>(function(){window._cf_chl_opt = {cvId: '3', cZone: 'site.test'};})();</script>
</body></html>"""
DATADOME = """<html><head><title>site.test</title></head><body>
<iframe src="https://geo.captcha-delivery.com/captcha/?initialCid=abc" width="100%"></iframe>
</body></html>"""
HUMAN = """<html><head><title>Access to this page has been denied</title></head><body>
<div id="px-captcha"></div><p>Press &amp; Hold to confirm you are a human.</p></body></html>"""
# Ordinary pages that embed captcha widgets or mention captchas: never bot checks.
COMMENT_FORM = """<html><head><title>Post</title>
<script src="https://www.google.com/recaptcha/api.js"></script>
<script src="https://challenges.cloudflare.com/turnstile/v0/api.js"></script></head>
<body><article><h1>Post</h1><p>Short post.</p></article>
<form action="/comments"><div class="g-recaptcha"></div><div class="cf-turnstile"></div>
</form></body></html>"""
ABOUT_CAPTCHAS = """<html><head><title>How captchas work</title></head><body><article>
<p>Amazon's captcha form posts to validateCaptcha and Cloudflare sets _cf_chl_opt; DataDome
loads captcha-delivery.com and HUMAN draws px-captcha.</p></article></body></html>"""


@pytest.mark.parametrize(("page", "vendor"), [(AMAZON, "amazon"), (CLOUDFLARE, "cloudflare"),
                                             (DATADOME, "datadome"), (HUMAN, "human")])  # fmt: skip
def test_known_bot_checks_are_recognized(page, vendor):
    assert meta.bot_challenge(page) == vendor


@pytest.mark.parametrize("page", [COMMENT_FORM, ABOUT_CAPTCHAS, ARTICLE])
def test_ordinary_pages_are_not_bot_checks(page):
    assert meta.bot_challenge(page) is None


@pytest.mark.parametrize("page", [AMAZON, CLOUDFLARE, DATADOME, HUMAN])
def test_a_bot_check_page_is_refused_and_never_charged(tmp_path, page):
    fac = FakeFacilitator()
    site = {"shop.test/robots.txt": ROBOTS_OK,
            "shop.test/p": httpx.Response(200, text=page, headers=HTML)}
    r = _paid(_client(tmp_path, site, fac), "https://shop.test/p")
    assert r.status_code == 422 and r.json()["error"] == "bot_check", r.text
    assert r.json()["charged"] is False and fac.calls_to("settle") == []


def test_a_long_page_carrying_a_marker_is_still_delivered(tmp_path):
    # The marker alone is not enough: real content beyond a bot-check page's size is delivered.
    body = "<p>" + "real content " * 200 + "</p>"
    page = AMAZON.replace("</body>", f"<article>{body}</article></body>")
    fac = FakeFacilitator()
    site = {"shop.test/robots.txt": ROBOTS_OK,
            "shop.test/p": httpx.Response(200, text=page, headers=HTML)}
    r = _paid(_client(tmp_path, site, fac), "https://shop.test/p")
    assert r.status_code == 200 and len(fac.calls_to("settle")) == 1


@pytest.mark.parametrize("page", [COMMENT_FORM, "<html><body><p>Hi.</p></body></html>"])
def test_short_genuine_pages_are_delivered_and_paid(tmp_path, page):
    fac = FakeFacilitator()
    site = {"blog.test/robots.txt": ROBOTS_OK,
            "blog.test/p": httpx.Response(200, text=page, headers=HTML)}
    r = _paid(_client(tmp_path, site, fac), "https://blog.test/p")
    assert r.status_code == 200 and r.json()["word_count"] >= 1
    assert len(fac.calls_to("settle")) == 1


def test_the_delivered_page_carries_its_metadata(tmp_path):
    fac = FakeFacilitator()
    site = {"news.test/robots.txt": ROBOTS_OK,
            "news.test/a": httpx.Response(200, text=ARTICLE, headers=HTML)}
    body = _paid(_client(tmp_path, site, fac), "https://news.test/a").json()
    assert body["metadata"]["authors"] == ["Ada Lovelace"]
    assert body["metadata"]["canonical_url"] == "https://news.test/articles/paying-per-call"
    assert body["metadata"]["published_at"] == "2026-11-20T09:30:00Z"
    # Existing fields are unchanged.
    assert body["title"] == "Paying per call" and "Agents pay per call" in body["content_markdown"]
    assert set(svc.OUTPUT_SCHEMA["required"]) <= set(body)


def test_a_plain_text_page_has_empty_metadata(tmp_path):
    fac = FakeFacilitator()
    site = {"raw.test/robots.txt": ROBOTS_OK, "raw.test/r.md": httpx.Response(
        200, text="# Readme\n\nText.", headers={"content-type": "text/plain"})}  # fmt: skip
    body = _paid(_client(tmp_path, site, fac), "https://raw.test/r.md").json()
    assert body["metadata"] == meta.empty_metadata()


def test_the_guide_and_schema_document_the_new_fields_and_refusal():
    settings = svc.Settings(pay_to=PAY_TO, resource_url="http://testserver/v1/page",
                            data_dir=Path("/tmp/unused"))  # fmt: skip
    guide = svc._page_guide(settings)
    assert "bot_check" in guide and "canonical_url" in guide and "published_at" in guide
    props = svc.OUTPUT_SCHEMA["properties"]["metadata"]["properties"]
    assert set(props) == set(meta.FIELDS)
