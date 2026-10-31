"""HTML to readable markdown with the standard library only.

Keeps a page's readable content (title, headings, paragraphs, lists, quotes, code, tables as
markdown tables, links resolved to absolute URLs) and drops what an agent can't use: scripts,
styles, navigation, headers, footers, forms, sidebars and anything hidden. When a page marks its
main content (`<main>`, `<article>` or `role="main"`), only that part is kept.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin

SKIP = {
    "script", "style", "noscript", "template", "svg", "canvas", "iframe", "nav", "header",
    "footer", "aside", "form", "button", "select", "option", "label", "dialog", "menu",
}  # fmt: skip
VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "base", "col"}
BLOCK = {
    "p", "div", "section", "article", "main", "li", "ul", "ol", "table", "tr", "blockquote",
    "pre", "h1", "h2", "h3", "h4", "h5", "h6", "dl", "dt", "dd", "figure", "figcaption", "br",
    "hr",
}  # fmt: skip
# In a table cell, any of these makes it a layout table; so do two or more paragraphs/divs (one
# wrapping <p> per cell is common in data tables, e.g. Sphinx docs).
LAYOUT = {"ul", "ol", "table", "blockquote", "pre", "h1", "h2", "h3", "h4", "h5", "h6"}
SKIP_ROLES = {"navigation", "banner", "contentinfo", "complementary", "search", "dialog"}
NOISE = re.compile(r"(^|[\s_-])(cookie|consent|newsletter|subscribe|share|social|breadcrumb)")


class _Table:
    """Rows of cells collected while inside a <table>. A data table (cells of inline text) renders
    as a markdown table; a layout table (cells holding paragraphs, lists, headings or tables, as
    whole pages built from tables do) renders its cells as ordinary blocks."""

    def __init__(self) -> None:
        self.rows: list[list[str]] = []
        self.row: list[str] | None = None
        self.cell: list[str] | None = None
        self.caption: list[str] = []
        self.layout = False
        self.cell_blocks = 0  # paragraphs/divs in the current cell

    def end_cell(self) -> None:
        if self.cell is not None:
            if self.row is None:
                self.row = []
            self.row.append("".join(self.cell))
            self.cell = None

    def end_row(self) -> None:
        self.end_cell()
        if self.row:
            self.rows.append(self.row)
        self.row = None

    def markdown(self) -> str:
        self.end_row()
        caption = re.sub(r"\s+", " ", "".join(self.caption)).strip()
        if self.layout:
            blocks = [cell.strip() for row in self.rows for cell in row if cell.strip()]
            return "\n\n".join(([caption] if caption else []) + blocks)
        rows = [[re.sub(r"\s+", " ", c).strip().replace("|", "\\|") for c in r] for r in self.rows]
        rows = [r for r in rows if any(r)]
        if not rows:
            return caption
        width = max(len(r) for r in rows)
        lines = ["| " + " | ".join(r + [""] * (width - len(r))) + " |" for r in rows]
        lines.insert(1, "|" + " --- |" * width)
        return "\n".join(([caption, ""] if caption else []) + lines)


class _Parser(HTMLParser):
    def __init__(self, base_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self._tables: list[_Table] = []
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
        if self._tables and self._tables[-1].cell is not None:
            self._tables[-1].cell.append(text)  # cell text joins its row, not the page
            return
        if self._tables:
            self._tables[-1].caption.append(text)  # <caption>, or whitespace between cells
            return
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
            elif tag == "base" and a.get("href") and not self._skip:
                self.base_url = urljoin(self.base_url, a["href"])
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
        if self._tables and self._tables[-1].cell is not None:
            t = self._tables[-1]
            t.cell_blocks += tag in ("p", "div")
            t.layout = t.layout or tag in LAYOUT or t.cell_blocks > 1
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
        elif tag == "a" and (href := self._link(a.get("href", ""))):
            self._href = href
            self._emit("[")
        elif tag == "table":
            self._tables.append(_Table())
        elif tag == "tr" and self._tables:
            self._tables[-1].end_row()
            self._tables[-1].row = []
        elif tag in ("td", "th") and self._tables:
            self._tables[-1].end_cell()
            self._tables[-1].cell = []
            self._tables[-1].cell_blocks = 0

    def _link(self, href: str) -> str | None:
        """The absolute http(s) URL a link points to, or None (fragments, mailto:, script)."""
        href = href.strip()
        if not href or href.startswith("#"):
            return None
        url = urljoin(self.base_url, href) if self.base_url else href
        return url if url.startswith(("http://", "https://")) else None

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
        elif tag in ("td", "th") and self._tables:
            self._tables[-1].end_cell()
        elif tag == "tr" and self._tables:
            self._tables[-1].end_row()
        elif tag == "table" and self._tables:
            table = self._tables.pop().markdown()
            if table:
                self._emit(f"\n\n{table}\n\n")
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


def html_to_markdown(html: str, base_url: str = "") -> tuple[str, str]:
    """(title, markdown) for an HTML document; links resolve against `base_url` (the page's
    final URL) or the page's own `<base href>`."""
    p = _Parser(base_url)
    p.feed(html)
    p.close()
    main = _tidy(p.main) if p.main else ""
    body = _tidy(p.out)
    # Prefer the marked main content unless it is implausibly small next to the page.
    content = main if main and len(main) >= 0.25 * len(body) else body
    for doc in _embedded_documents(html):
        _, embedded = html_to_markdown(doc, p.base_url)
        if len(embedded) > len(content):
            content = f"{content}\n\n{embedded}" if content else embedded
    return re.sub(r"\s+", " ", p.title).strip(), content
