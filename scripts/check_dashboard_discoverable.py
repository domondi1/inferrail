#!/usr/bin/env python3
"""CI check: after `app/dist` is built, confirm the Python backend's own
discovery logic actually finds it (not just that the file exists) — see
inferrail.dashboard.find_dashboard_dist and
docs/adr/0017-dashboard-in-app-directory.md.
"""

import sys

from inferrail.dashboard import find_dashboard_dist


def main() -> int:
    found = find_dashboard_dist()
    if found is None or found.name != "dist":
        print(f"error: find_dashboard_dist() returned {found!r}, expected an 'app/dist' path")
        return 1
    print(f"ok: dashboard discovered at {found}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
