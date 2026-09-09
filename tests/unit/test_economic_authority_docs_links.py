"""Guards `docs/capabilities/economic-authority.md`, its ADR, and the
README's reference to both against link rot and malformed schemas --
this capability's documentation-cannot-silently-drift guarantee.

Pure file-system checks, no network access and no hosted extra required
-- this test always runs as part of the main `pytest` job.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_DOC = REPO_ROOT / "docs" / "capabilities" / "economic-authority.md"
ADR = (
    REPO_ROOT
    / "docs"
    / "adr"
    / "0011-economic-authority-a2a-capability-and-nonce-keyed-recovery.md"
)
README = REPO_ROOT / "README.md"
SCHEMA_DIR = REPO_ROOT / "docs" / "capabilities" / "schemas" / "economic-authority"

_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")


def _relative_links(markdown_path: Path) -> list[str]:
    """Every markdown link target that is not an absolute URL or a
    same-page anchor."""
    text = markdown_path.read_text()
    links = []
    for target in _MD_LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "#")):
            continue
        links.append(target)
    return links


@pytest.mark.parametrize("markdown_path", [CAPABILITY_DOC, ADR, README])
def test_every_relative_link_resolves_to_a_real_file(markdown_path: Path):
    assert markdown_path.is_file(), f"expected doc file missing: {markdown_path}"
    for target in _relative_links(markdown_path):
        # Strip a trailing #anchor, if any, before resolving the file part.
        file_part = target.split("#", 1)[0]
        if not file_part:
            continue
        resolved = (markdown_path.parent / file_part).resolve()
        assert resolved.exists(), (
            f"{markdown_path.relative_to(REPO_ROOT)} links to {target!r}, "
            f"which does not resolve to a real file or directory ({resolved})"
        )


def test_capability_doc_links_every_schema_file():
    """Every schema this capability ships must actually be referenced
    from its own capability doc -- otherwise a schema could silently
    become undiscoverable prose-only trivia."""
    schema_files = sorted(p.name for p in SCHEMA_DIR.glob("*.schema.json"))
    assert schema_files, "expected at least one schema file"
    doc_text = CAPABILITY_DOC.read_text()
    for name in schema_files:
        assert name in doc_text, f"{name} is never linked from {CAPABILITY_DOC.name}"


def test_readme_references_the_capability_doc_and_example():
    text = README.read_text()
    assert "docs/capabilities/economic-authority.md" in text
    assert "examples/economic_authority_session.py" in text


@pytest.mark.parametrize("schema_path", sorted(SCHEMA_DIR.glob("*.schema.json")))
def test_every_schema_file_is_well_formed_json_schema(schema_path: Path):
    schema = json.loads(schema_path.read_text())
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft7Validator.check_schema(schema)


def test_example_script_is_referenced_and_exists():
    example_path = REPO_ROOT / "examples" / "economic_authority_session.py"
    assert example_path.is_file()
    assert "economic_authority_session.py" in CAPABILITY_DOC.read_text()
