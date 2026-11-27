"""Web Read reads PDFs: the text layer becomes the content, the document info becomes metadata.

Proves: a PDF with text is delivered (and so charged) with title, author and dates; a scan with
no text layer, a broken PDF and a PDF over the size cap are refused for free; a long PDF is read
up to MAX_PAGES with a note; READ_PDF=0 restores the old refusal; HTML reading is unchanged.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("pypdf")

ROOT = Path(__file__).resolve().parents[3]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


svc = sys.modules.get("web_read_service") or _load(
    "web_read_service", ROOT / "hosted" / "web_read" / "service.py"
)
fetch = sys.modules["web_read_fetch"]
pdf = sys.modules.get("web_read_pdf") or _load(
    "web_read_pdf", ROOT / "hosted" / "web_read" / "pdf.py"
)


def make_pdf(pages: list[str], title: str = "", author: str = "", created: str = "") -> bytes:
    """A minimal, valid PDF with one line of Helvetica text per page (empty = no text layer)."""
    objs: list[bytes] = []
    kids = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    info_id = font_id + 1
    for i, text in enumerate(pages):
        page_id, content_id = 3 + 2 * i, 4 + 2 * i
        kids.append(f"{page_id} 0 R")
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode() if text else b""
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode()
        )
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    body = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {n_pages} >>".encode(), *objs,
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            f"<< /Title ({title}) /Author ({author}) "
            f"/CreationDate ({created}) >>".encode()]  # fmt: skip
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(body, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(body) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    trailer = f"<< /Size {len(body) + 1} /Root 1 0 R /Info {info_id} 0 R >>"
    out += f"trailer\n{trailer}\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


PDF = {"content-type": "application/pdf"}
ARTICLE = "<html><head><title>T</title></head><body><main><p>Hello world.</p></main></body></html>"


def _reader(routes: dict[str, httpx.Response], pdf_on: bool = True) -> Any:
    routes = {
        "docs.test/robots.txt": httpx.Response(200, text="User-agent: *\nAllow: /\n"),
        **routes,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        r = routes.get(f"{request.url.host}{request.url.path}")
        return (
            httpx.Response(404)
            if r is None
            else httpx.Response(r.status_code, headers=r.headers, content=r.content)
        )

    async def dns(host: str, port: int) -> list[str]:
        return ["93.184.216.34"]

    return fetch.Fetcher("test", transport=httpx.MockTransport(handler), resolve=dns, pdf=pdf_on)


def _doc(name: str, body: bytes, pdf_on: bool = True) -> tuple[int, dict[str, Any]]:
    """Read https://docs.test/<name>, served as a PDF."""
    served = {f"docs.test/{name}": httpx.Response(200, content=body, headers=PDF)}
    return _read(_reader(served, pdf_on=pdf_on), f"https://docs.test/{name}")


def _read(reader: Any, url: str) -> tuple[int, dict[str, Any]]:
    events: list[tuple[str, str]] = []
    return asyncio.run(svc.read_page(reader, url, lambda *e: events.append(e)))


def test_pdf_text_and_document_info_are_delivered():
    body = make_pdf(["Attention is all you need", "Second page text"], title="A Paper",
                    author="Ada Lovelace", created="D:20260101120000Z")  # fmt: skip
    status, row = _doc("p.pdf", body)
    assert status == 200, row
    assert (
        "Attention is all you need" in row["content_markdown"]
        and "Second page" in row["content_markdown"]
    )
    assert row["title"] == "A Paper"
    assert row["metadata"]["authors"] == ["Ada Lovelace"] and row["metadata"]["type"] == "PDF"
    assert row["metadata"]["published_at"].startswith("2026-01-01")
    assert len(row["content_sha256"]) == 64 and row["word_count"] > 0


def test_scanned_pdf_without_text_is_free():
    body = make_pdf(["", ""])
    status, row = _doc("s.pdf", body)
    assert (status, row["error"], row["charged"]) == (422, "no_readable_content", False)


def test_broken_pdf_is_free():
    status, row = _doc("b.pdf", b"%PDF-1.4 garbage")
    assert (status, row["charged"]) == (422, False) and row["error"] in (
        "unreadable_pdf",
        "no_readable_content",
    )


def test_pdf_over_the_cap_is_refused_free():
    big = b"%PDF-1.4\n" + b"0" * (fetch.MAX_PDF_BYTES + 10)
    status, row = _doc("big.pdf", big)
    assert row["error"] == "too_large" and row["charged"] is False and status != 200


def test_long_pdf_is_read_up_to_the_page_cap_with_a_note(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PAGES", 3)
    title, content, _ = pdf.parse(make_pdf([f"page {i}" for i in range(6)]), max_pages=3)
    assert "page 2" in content and "page 3" not in content
    assert "[Only the first 3 of 6 pages were read.]" in content


def test_pdf_off_keeps_the_old_refusal():
    body = make_pdf(["hello"])
    status, row = _doc("p.pdf", body, pdf_on=False)
    assert (row["error"], row["charged"]) == ("unsupported_content_type", False)


def test_settings_pdf_flag_defaults_on_and_can_be_turned_off(tmp_path):
    env = {
        "READ_PAY_TO": "0x" + "7" * 40,
        "READ_RESOURCE_URL": "http://t/v1/page",
        "READ_DATA_DIR": str(tmp_path),
    }
    assert svc.Settings.from_env(env).pdf is True
    assert svc.Settings.from_env({**env, "READ_PDF": "0"}).pdf is False


def test_html_is_unchanged_with_pdf_on():
    html = {"content-type": "text/html; charset=utf-8"}
    status, row = _read(
        _reader({"docs.test/a": httpx.Response(200, text=ARTICLE, headers=html)}),
        "https://docs.test/a",
    )
    assert (
        status == 200
        and "Hello world." in row["content_markdown"]
        and row["metadata"]["type"] != "PDF"
    )


def test_decompression_is_capped():
    import pypdf

    config = pypdf.get_configuration()
    assert config.zlib_maximum_output_length <= pdf.DECOMPRESS_MAX
    assert config.maximum_declared_stream_length <= pdf.DECOMPRESS_MAX
