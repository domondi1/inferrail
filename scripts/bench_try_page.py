#!/usr/bin/env python3
"""Measure the /try/ page's first-use latency against a local Cost Gateway.

Starts a throwaway gateway (fresh data dir) and a static server for
docs/, then drives the real page in headless Chromium and reports:

- first contentful paint and DOMContentLoaded,
- click "Try free" -> trial panel ready (the page's own
  `inferrail:trial-ready` User Timing measure),
- click "Send a demo request" -> receipt shown (`inferrail:first-demo`),
- gateway requests in the first 6 s after the click, and how many of
  them were CORS preflights (counted from the gateway's own access log),
- requests an idle, visible trial tab makes per minute.

Optional `--rtt-ms` emulates a round-trip latency via Chrome DevTools,
which is where request count and connection reuse start to matter.

Needs Playwright and a Chromium build, neither of which is a repository
dependency: `pip install playwright && playwright install chromium`.
Never touches the production service.

    python scripts/bench_try_page.py --runs 5 --rtt-ms 100
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GATEWAY_PORT = 8499
STATIC_PORT = 5599


def _request_log(path: Path) -> list[dict]:
    lines = []
    for raw in path.read_text().splitlines():
        try:
            entry = json.loads(raw)
        except ValueError:
            continue
        if entry.get("event") == "request":
            lines.append(entry)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--rtt-ms", type=int, default=0)
    parser.add_argument("--idle-seconds", type=int, default=30)
    parser.add_argument("--chromium", help="path to a Chromium executable (optional)")
    args = parser.parse_args()

    from playwright.sync_api import sync_playwright

    data_dir = Path(tempfile.mkdtemp(prefix="bench-try-"))
    log_path = data_dir / "gateway.log"
    env = dict(
        os.environ,
        PYTHONPATH=f"{REPO / 'hosted' / 'cost_gateway'}{os.pathsep}{REPO / 'src'}",
        COST_GATEWAY_ISSUE_MAX_PER_IP="1000",
        COST_GATEWAY_CLIENT_IP_HEADER="",
    )
    with log_path.open("w") as log_file:
        gateway = subprocess.Popen(
            [sys.executable, str(REPO / "hosted/cost_gateway/service.py"), str(data_dir / "data"),
             str(GATEWAY_PORT)],
            env=env, stdout=log_file, stderr=subprocess.STDOUT,
        )
    static = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(STATIC_PORT), "--directory", str(REPO)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    url = (f"http://127.0.0.1:{STATIC_PORT}/docs/try/"
           f"?base_url=http://127.0.0.1:{GATEWAY_PORT}")
    results: dict[str, list[float]] = {
        k: [] for k in ("fcp_ms", "dcl_ms", "ready_ms", "first_demo_ms", "requests_6s",
                        "preflights_6s")
    }
    idle_per_minute = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=args.chromium) if args.chromium \
                else p.chromium.launch()
            for run in range(args.runs):
                context = browser.new_context()
                page = context.new_page()
                if args.rtt_ms:
                    cdp = context.new_cdp_session(page)
                    cdp.send("Network.enable")
                    cdp.send("Network.emulateNetworkConditions", {
                        "offline": False, "latency": args.rtt_ms,
                        "downloadThroughput": -1, "uploadThroughput": -1,
                    })
                page.goto(url, wait_until="load")
                page.wait_for_timeout(200)
                paint = page.evaluate("""() => ({
                    fcp: (performance.getEntriesByName('first-contentful-paint')[0]
                          || {}).startTime,
                    dcl: performance.getEntriesByType('navigation')[0]
                          .domContentLoadedEventEnd })""")
                seen = len(_request_log(log_path))
                started = time.monotonic()
                page.click("#start-trial-btn")
                page.wait_for_selector("#trial-panel:not(.hidden)")
                page.click("#send-demo-btn")
                page.wait_for_selector("#test-result .card")
                measures = page.evaluate("""() => Object.fromEntries(
                    performance.getEntriesByType('measure').map(m => [m.name, m.duration]))""")
                page.wait_for_timeout(max(0, int((6 - (time.monotonic() - started)) * 1000)))
                burst = _request_log(log_path)[seen:]
                results["fcp_ms"].append(paint["fcp"])
                results["dcl_ms"].append(paint["dcl"])
                results["ready_ms"].append(measures["inferrail:trial-ready"])
                results["first_demo_ms"].append(measures["inferrail:first-demo"])
                results["requests_6s"].append(len(burst))
                results["preflights_6s"].append(sum(1 for e in burst if e["method"] == "OPTIONS"))
                if run == args.runs - 1 and args.idle_seconds:
                    seen = len(_request_log(log_path))
                    page.wait_for_timeout(args.idle_seconds * 1000)
                    idle = len(_request_log(log_path)) - seen
                    idle_per_minute = idle * 60 / args.idle_seconds
                context.close()
            browser.close()
    finally:
        gateway.terminate()
        static.terminate()

    print(f"/try/ first-use latency -- runs={args.runs}, emulated RTT={args.rtt_ms} ms (median)")
    for name, values in results.items():
        values = [v for v in values if v is not None]
        print(f"  {name:15s} {statistics.median(values):8.1f}   all={[round(v) for v in values]}")
    if idle_per_minute is not None:
        print(f"  idle requests/minute (visible tab): {idle_per_minute:.0f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
