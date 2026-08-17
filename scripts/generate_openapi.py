"""Generate `openapi.json` from the live FastAPI app.

Run directly to regenerate the committed snapshot:

    python scripts/generate_openapi.py

CI (`.github/workflows/ci.yml`) runs this, then `git diff --exit-code
openapi.json` — a non-empty diff means the committed snapshot has drifted
from the live app and fails the build. Uses the quickstart config
(`inferrail.config.quickstart`) so this needs no `inferrail.yaml`.
`create_app` eagerly constructs the configured provider, which requires
*some* non-empty `OPENAI_API_KEY` to exist in the environment even though
OpenAPI generation never calls it — a placeholder is supplied here only
if the caller hasn't already set a real one, so this works unattended in
CI without ever making a network call.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from inferrail.config.quickstart import build_quickstart_config  # noqa: E402
from inferrail.gateway.app import create_app  # noqa: E402

_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "openapi.json"


def generate() -> dict[str, object]:
    os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-for-schema-generation-only")
    app = create_app(build_quickstart_config())
    schema: dict[str, object] = app.openapi()
    return schema


def main() -> None:
    schema = generate()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    _OUTPUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
