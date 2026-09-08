"""Subprocess helper for crash-recovery testing of EconomicAuthorityStore.

Not a test module itself (leading underscore keeps pytest from collecting
it). Performs exactly one committed mutation against the given database,
then hard-kills the process with `os._exit`, which skips atexit handlers,
`finally` blocks, and any buffered output -- proving durability against a
real, uncontrolled process death rather than a clean shutdown.

Usage: python3 _a2a_economic_authority_crash_helper.py <db_path> <boundary>
"""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

_HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "a2a_economic_authority"
if str(_HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(_HOSTED_DIR))

from core import EconomicAuthorityStore  # noqa: E402


def main() -> None:
    db_path, boundary = sys.argv[1], sys.argv[2]
    store = EconomicAuthorityStore(db_path)

    if boundary == "after_reserve":
        store.reserve("evt:reserve", "root", "child-1", "worker", Decimal("0.30"))
    elif boundary == "after_consume":
        store.consume("evt:consume", "child-1", Decimal("0.10"))
    elif boundary == "after_settle":
        store.settle("evt:settle", "child-1", "SUCCESS")
    else:
        raise ValueError(f"unknown boundary: {boundary}")

    os._exit(1)  # deliberate hard kill, see module docstring


if __name__ == "__main__":
    main()
