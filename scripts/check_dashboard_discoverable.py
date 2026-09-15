#!/usr/bin/env python3
"""CI check: after `app/dist` is built, confirm the Python backend's own
discovery logic actually finds a dashboard build — see
inferrail.dashboard.find_dashboard_dist and
docs/adr/0017-dashboard-in-app-directory.md.

Accepts either `app/dist` (the git-checkout fallback) or a bundled
`dashboard_static/` (docs/adr/0018) as a valid result: `pip install -e .`
also runs the wheel-packaging build hook when Node is on PATH -- as it
is in this job -- so which one `find_dashboard_dist()` actually returns
depends on install/build ordering, not on anything this check needs to
pin down. Either is proof discovery works.
"""

import sys

from inferrail.dashboard import find_dashboard_dist

_VALID_NAMES = {"dist", "dashboard_static"}


def main() -> int:
    found = find_dashboard_dist()
    if found is None or found.name not in _VALID_NAMES:
        print(
            f"error: find_dashboard_dist() returned {found!r}, expected an "
            f"'app/dist' or bundled 'dashboard_static' path"
        )
        return 1
    print(f"ok: dashboard discovered at {found}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
