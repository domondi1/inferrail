"""Generate `config.schema.json` from `InferrailConfig` (pydantic).

Run directly to regenerate the committed snapshot:

    python scripts/generate_config_schema.py

CI runs this, then `git diff --exit-code config.schema.json` — the
schema is generated from the actual pydantic model
(`inferrail.config.models.InferrailConfig`), not hand-maintained, so it
can't silently drift from what `inferrail config check` actually
validates.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from inferrail.config.models import InferrailConfig  # noqa: E402

_OUTPUT_PATH = Path(__file__).resolve().parent.parent / "config.schema.json"


def generate() -> dict[str, object]:
    schema = InferrailConfig.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "Inferrail configuration (inferrail.yaml)"
    return schema


def main() -> None:
    schema = generate()
    text = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    _OUTPUT_PATH.write_text(text, encoding="utf-8")
    print(f"Wrote {_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
