# 0018. Bundling the built dashboard into the PyPI wheel

## Status

Accepted

## Context

`docs/adr/0017-dashboard-in-app-directory.md`'s "Consequences" flagged a
known gap: the dashboard (`app/`) was buildable and servable from a git
checkout, but `pip install inferrail` alone shipped no dashboard at
all — `inferrail.dashboard.find_dashboard_dist()`'s checkout-relative
fallback (`app/dist` found by walking up from the installed module) only
resolves when the package is installed *from* a checkout, never from a
wheel downloaded off PyPI. This ADR closes that gap.

## Decision

**A custom hatchling build hook (`hatch_build.py`, registered via
`[tool.hatch.build.hooks.custom]`) runs `npm ci && npm run build` in
`app/` and copies the result into `src/inferrail/dashboard_static/`
before the wheel's file list is finalized**, using `build_data
["force_include"]` rather than relying on hatchling's default
`packages` directory walk — see "Why `force_include`" below.
`inferrail.dashboard.find_dashboard_dist()` already checked a
`dashboard_static/` path inside the installed package first (reserved
for exactly this since ADR-0017), so no runtime code changed at all —
only wheel-build-time code was added.

**The hook never fails the build.** If `app/` isn't a full checkout
(no `app/package.json`), if `npm` isn't on `PATH`, if the npm build
itself fails, or if it succeeds without producing `index.html`, the
hook prints a one-line reason to stderr and returns — the wheel still
builds, just without a bundled dashboard, identical to today's
behavior. This matters because most environments that build this wheel
*from an sdist* (rather than downloading a prebuilt wheel from PyPI)
have no reason to have Node installed, and a packaging improvement must
never be the reason `pip install inferrail` fails for someone who lacks
it. Only builds where Node happens to be present (this project's own
CI, a maintainer's machine) actually get a bundled dashboard baked into
the wheel they produce.

**Why `force_include` instead of just writing files under
`src/inferrail/` and letting the existing `packages = ["src/inferrail",
...]` config pick them up:** the first implementation attempt did
exactly that, and it silently failed — `dashboard_static/` must be
gitignored (it's a generated build artifact, never committed, same as
`app/dist` itself), and hatchling's default wheel file selection
*also* respects `.gitignore` for `packages`-mode discovery. A path
correctly excluded from version control was therefore also excluded
from the wheel, even though the hook had genuinely written real files
there moments earlier in the same build. `build_data["force_include"]`
is hatchling's documented mechanism for exactly this situation — a
hook-generated, intentionally-gitignored path that must still ship —
and bypasses the VCS-aware selection entirely for that one path.
Verified by building a real wheel both before and after this fix: the
files existed on disk in both cases, but only appeared in the wheel
archive after switching to `force_include`.

**A pre-existing, unrelated packaging gap was found and fixed in the
same pass:** hatchling's default sdist target does not respect
`app/.gitignore` either, so a plain `python -m build` was including
`app/node_modules` (real dependency bloat, sizable) and any
locally-built `app/dist` in the source tarball — present since `app/`
was first added (ADR-0017), not something this change introduced.
Fixed via an explicit `[tool.hatch.build.targets.sdist]` `exclude`,
rather than relying on gitignore-based inference a second time.

**Tests:** `tests/unit/test_hatch_build.py` covers the hook's own
skip-gracefully logic directly (mocked `npm`/`subprocess`, since these
paths must be exercised without actually depending on Node being
present in every test environment) — not the real end-to-end npm build,
which would be a weak, mocked proof of the thing that actually matters.
That real path is instead verified by the `dashboard` CI job (already
has Node set up): build a real wheel, assert
`inferrail/dashboard_static/index.html` is actually present in the
archive, install that wheel into a clean venv, and confirm
`find_dashboard_dist()` finds it there — the same sequence this ADR's
own author ran manually against a real `pip install` before writing
this file.

## Consequences

- `pip install inferrail` (from a prebuilt PyPI wheel built by this
  project's own CI, which has Node) now ships a working dashboard —
  ADR-0017's "Known gap" is closed for that path.
- `pip install inferrail` on a system that ends up building from the
  sdist (rare, but possible — e.g. `--no-binary`, or no matching
  prebuilt wheel for that platform/Python combination) still installs
  successfully without Node, just without a dashboard — no regression,
  no new failure mode, matching every existing `--app-mode` guarantee.
- `hatchling` is added to the `dev` extra purely so
  `test_hatch_build.py` can import `BuildHookInterface` — it was
  already an implicit build-time dependency via `[build-system]
  .requires`, just not otherwise importable in a normal dev/test
  environment because of pip's PEP 517 build isolation.
- **Still not done, deliberately out of scope for this pass:** the
  *actual* PyPI-published wheel (`publish.yml`'s `build` job) and the
  three-OS `platform-verify.yml`/`wheel-smoke` jobs don't set up Node
  yet, so neither currently produces a dashboard-bundled artifact —
  only this repository's own `dashboard` CI job (which already has
  Node) demonstrates the real bundling working. Wiring Node into those
  two higher-stakes, more heavily-reviewed workflows is a deliberate
  follow-up decision, not an oversight — see `PROGRESS.md`'s record of
  this pass for the reasoning.
