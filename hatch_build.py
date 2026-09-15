"""Hatchling build hook: bundles the built dashboard (`app/dist`) into
the wheel as `src/inferrail/dashboard_static/` — closes the gap
`docs/adr/0017-dashboard-in-app-directory.md`'s "Consequences" section
flags (`pip install inferrail` alone shipping no dashboard).

Runs only for the wheel target, and **never fails the build**: most
build environments that produce the wheel someone actually
`pip install`s (this project's own CI, a maintainer's machine) have
Node, but an environment building from an sdist without Node must still
get a working (dashboard-less) wheel, not a broken install — see
`inferrail.dashboard.find_dashboard_dist`, which already treats a
missing dashboard as a normal, non-error state at runtime.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class DashboardBuildHook(BuildHookInterface):
    def initialize(self, version: str, build_data: dict) -> None:
        if self.target_name != "wheel":
            return

        root = Path(self.root)
        app_dir = root / "app"
        dist_dir = app_dir / "dist"
        target_dir = root / "src" / "inferrail" / "dashboard_static"

        if not (app_dir / "package.json").is_file():
            return  # not a full checkout (e.g. a stripped sdist) -- never fatal

        npm = shutil.which("npm")
        if npm is None:
            self._skip("npm not found on PATH")
            return

        try:
            subprocess.run([npm, "ci"], cwd=app_dir, check=True)
            subprocess.run([npm, "run", "build"], cwd=app_dir, check=True)
        except subprocess.CalledProcessError as exc:
            self._skip(f"dashboard build failed ({exc})")
            return

        if not (dist_dir / "index.html").is_file():
            self._skip("npm run build did not produce app/dist/index.html")
            return

        if target_dir.exists():
            shutil.rmtree(target_dir)
        shutil.copytree(dist_dir, target_dir)

        # `target_dir` is deliberately gitignored (never committed --
        # it's a build artifact), but hatchling's default wheel file
        # selection respects .gitignore too, which would silently
        # exclude it despite these files genuinely existing on disk at
        # build time. `force_include` bypasses that VCS-aware selection
        # for exactly this kind of hook-generated path.
        build_data.setdefault("force_include", {})[str(target_dir)] = (
            "inferrail/dashboard_static"
        )
        print(f"hatch_build.py: bundled dashboard from {dist_dir} into {target_dir}")

    @staticmethod
    def _skip(reason: str) -> None:
        print(
            f"hatch_build.py: {reason} -- building the wheel without a bundled "
            "dashboard (normal for most build environments; see "
            "docs/adr/0017-dashboard-in-app-directory.md)",
            file=sys.stderr,
        )
