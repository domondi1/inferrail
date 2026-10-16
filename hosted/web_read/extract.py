"""HTML to readable markdown with the standard library only.

Keeps a page's readable content (title, headings, paragraphs, lists, quotes, code, table text,
links) and drops what an agent can't use: scripts, styles, navigation, headers, footers, forms,
sidebars and anything hidden. When a page marks its main content (`<main>`, `<article>` or
`role="main"`), only that part is kept.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser

SKIP = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "nav", "header",
    "footer", "aside", "form", "button", "select", "option", "label", "dialog", "menu",
}  # fmt: skip
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col"}
BLOCK = {
    "p", "div", "section", "article", "main", "li", "ul", "ol", "table", "tr", "blockquote",
    "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dl", "dt", "dd", "figure", "figcaption", "br",
    "hr", "td", "th",
}  # fmt: skip
SKIP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog"}
NOISE = re.compile(r"(^|[\s_-])(cookie|consent|newsletter|subscribe|share|social|breadcrumb)")


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip = 0  # depth inside a dropped element
        self._stack: list[tuple[str, bool]] = []  # (tag, opened a skip)
        self.out: list[str] = []  # whole-page chunks
        self.main: list[str] | None = None  # chunks inside the first main/article
        self._main_depth = 0
        self._href: str | None = None
        self._list: list[str] = []
        self._pre = 0

    def _emit(self, text: str) -> None:
        self.out.append(text)
        if self._main_depth and self.main is not None:
            self.main.append(text)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "title":
            self._in_title = True
        hidden = (
            "hidden" in a
            or a.get("aria-hidden") == "true"
            or "display:none" in (a.get("style", "").replace(" ", ""))
        )
        noisy = bool(NOISE.search(f" {a.get('class', '')} {a.get('id', '')}".lower()))
        skip = tag in SKIP or a.get("role") in SKIP_ROLES or hidden or noisy
        if tag in VOID:
            if tag in ("br", "hr") and not self._skip:
                self._emit("\n")
            return
        self._stack.append((tag, skip))
        if skip:
            self._skip += 1
            return
        if self._skip:
            return
        if (tag in ("main", "article") or a.get("role") == "main") and self.main is None:
            self.main = []
        if self.main is not None and (tag in ("main", "article") or a.get("role") == "main"):
            self._main_depth += 1
        if tag in BLOCK:
            self._emit("\n\n")
        if re.fullmatch(r"h[1-6]", tag):
            self._emit("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self._list.append(tag)
        elif tag == "li":
            self._emit("1. " if self._list and self._list[-1] == "ol" else "- ")
        elif tag == "blockquote":
            self._emit("> ")
        elif tag == "pre":
            self._pre += 1
            self._emit("```\n")
        elif tag == "a" and a.get("href", "").startswith(("http://", "https://", "/")):
            self._href = a["href"]
            self._emit("[")
        elif tag in ("td", "th"):
            self._emit(" | ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
        if tag in VOID:
            return
        # Pop to the matching tag; tolerate unclosed children.
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                for _, opened_skip in self._stack[i:]:
                    if opened_skip:
                        self._skip -= 1
                del self._stack[i:]
                break
        else:
            return
        if self._skip:
            return
        if tag == "a" and self._href is not None:
            self._emit(f"]({self._href})")
            self._href = None
        elif tag in ("ul", "ol") and self._list:
            self._list.pop()
        elif tag == "pre" and self._pre:
            self._pre -= 1
            self._emit("\n```")
        if tag in BLOCK:
            self._emit("\n\n")
        if (tag in ("main", "article")) and self._main_depth:
            self._main_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self._emit(data if self._pre else re.sub(r"\s+", " ", data))


def _tidy(chunks: list[str]) -> str:
    text = "".join(chunks)
    text = re.sub(r"\[\s*\]\([^)]*\)", "", text)  # links with no text
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(line.lstrip() if not line.startswith("    ") else line for line in lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_EMBEDDED_RICH_TEXT = re.compile(r'"richText":("(?:[^"\\]|\\.)*")')


def _embedded_documents(html: str) -> list[str]:
    """Rendered documents a page ships as JSON for client-side rendering (GitHub embeds a
    repository's README this way, so it isn't in the markup)."""
    out = []
    for m in _EMBEDDED_RICH_TEXT.finditer(html):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return out


def html_to_markdown(html: str) -> tuple[str, str]:
    """(title, markdown) for an HTML document."""
    p = _Parser()
    p.feed(html)
    p.close()
    main = _tidy(p.main) if p.main else ""
    body = _tidy(p.out)
    # Prefer the marked main content unless it is implausibly small next to the page.
    content = main if main and len(main) >= 0.25 * len(body) else body
    for doc in _embedded_documents(html):
        _, embedded = html_to_markdown(doc)
        if len(embedded) > len(content):
            content = f"{content}\n\n{embedded}" if content else embedded
    return re.sub(r"\s+", " ", p.title).strip(), content
