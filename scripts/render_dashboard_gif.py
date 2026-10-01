"""Capture a real gateway + dashboard run and render it as the README GIF.

The GIF tells one story: an AI job gets a $0.04 spending limit, its model
calls are recorded with their cost, and once the limit is reached the
remaining calls are blocked before they reach the model.

Two steps, both reproducible:

1. ``--capture`` installs ``inferrail`` (plus the ``openai`` SDK) into a
   fresh virtual environment, starts ``inferrail serve --app-mode`` from it
   with an isolated data directory, runs a small agent script that tags
   six model calls with one ``work_id`` and a per-run budget, and
   screenshots the real local dashboard with Playwright while those
   receipts arrive. The model upstream is a local stand-in speaking the
   OpenAI chat-completions wire format, so no API key is used and no
   provider is billed. Everything else (gateway, budgets, receipts,
   dashboard) is the installed package. By default the package is this
   checkout (``--package .``); pass ``--package inferrail`` to capture
   the latest PyPI release instead. Command output, screenshots and a
   ``capture.json`` land in ``docs/assets/dashboard-capture/``.
2. Rendering reads only those captured files and composes frames with
   Pillow. Terminal text comes from the captured stdout (the local API
   token and temporary paths are masked); dashboard frames are crops of
   the screenshots, scaled to fill the frame. The only addition is a
   caption strip above each frame, outside the product UI.

Usage (from a checkout; the install step needs network, and Node to
bundle the dashboard when capturing a checkout)::

    python -m pip install pillow playwright && python -m playwright install chromium
    python scripts/render_dashboard_gif.py --capture   # re-run, then render
    python scripts/render_dashboard_gif.py             # re-render from the committed capture

Outputs: ``docs/assets/inferrail-dashboard-demo.gif`` and
``docs/assets/inferrail-dashboard-demo-poster.png``. Pillow and Playwright
are dev-time tools here, not package dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
CAPTURE = ASSETS / "dashboard-capture"
SHOTS = CAPTURE / "screens"
GIF_PATH = ASSETS / "inferrail-dashboard-demo.gif"
POSTER_PATH = ASSETS / "inferrail-dashboard-demo-poster.png"

PORT = 8000
WORK_ID = "contract-review-42"
SERVE_CMD = "inferrail serve --app-mode --config inferrail.yaml"
SHOW_CMD = "grep Inferrail agent_run.py"
RUN_CMD = "python agent_run.py"

# The stand-in upstream: a provider named for what it is, priced with the
# built-in gpt-4o list price (an operator assertion recorded on each receipt).
CONFIG_YAML = """\
providers:
  stand-in:
    type: openai_compatible
    api_key_env: STAND_IN_KEY
    base_url: http://127.0.0.1:{upstream_port}/v1
    price_as: openai
routes:
  default:
    provider: stand-in
    model: gpt-4o
default_provider: stand-in
"""

AGENT_RUN = f'''\
from openai import APIStatusError, OpenAI

job = {{
    "X-Inferrail-Attribute-Work-Id": "{WORK_ID}",  # one AI job
    "X-Inferrail-Budget-Usd": "0.04",  # its spending limit, in dollars
}}
client = OpenAI(base_url="http://127.0.0.1:{PORT}/v1", api_key="unused", default_headers=job)
contract = open("contract.txt").read()

steps = ["extract clauses", "check liability", "check renewal",
         "compare to playbook", "draft summary", "draft reply"]
for step in steps:
    try:
        client.chat.completions.create(model="gpt-4o", max_tokens=400, messages=[
            {{"role": "user", "content": f"{{step}}:\\n{{contract}}"}}])
        print(f"{{step:<20}} answered")
    except APIStatusError as e:
        print(f"{{step:<20}} blocked ({{e.status_code}}): over budget, not sent to the model")
'''

# Synthetic input: roughly 2,000 tokens of placeholder contract text.
CONTRACT = "".join(
    f"Clause {i}. The supplier shall deliver the services described in schedule {i} "
    f"within the agreed period, subject to the limits in section {i + 1}.\n"
    for i in range(1, 60)
)

UPSTREAM_LATENCY_S = 0.7
COMPLETION_TOKENS = 300


class _StandIn(BaseHTTPRequestHandler):
    """Answers /v1/chat/completions with deterministic usage and no model."""

    def log_message(self, *args: Any) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        chars = sum(len(str(m.get("content", ""))) for m in body.get("messages", []))
        prompt = math.ceil(chars / 4)
        completion = min(int(body.get("max_tokens") or COMPLETION_TOKENS), COMPLETION_TOKENS)
        time.sleep(UPSTREAM_LATENCY_S)
        out = json.dumps(
            {
                "id": "chatcmpl-stand-in",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "(stand-in reply)"},
                    }
                ],
                "usage": {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                },
            }
        ).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for(url: str, timeout: float = 20) -> None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.2)
    sys.exit(f"timed out waiting for {url}")


def _git_label() -> str:
    sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO, capture_output=True, text=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--", "src", "app/src", "pyproject.toml"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return f"{sha}{'-dirty' if dirty else ''}"


def capture(package_spec: str) -> None:
    from playwright.sync_api import Page, sync_playwright

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            sys.exit(f"port {PORT} is in use; stop whatever is listening there first")
    SHOTS.mkdir(parents=True, exist_ok=True)
    for old in SHOTS.glob("*.png"):
        old.unlink()
    local = package_spec in (".", str(REPO))
    spec = str(REPO) if local else package_spec

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        venv = tmp / "venv"
        work = tmp / "work"
        work.mkdir()
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        subprocess.run(
            [
                str(bindir / "python"),
                "-m",
                "pip",
                "install",
                "-q",
                "--no-cache-dir",
                spec,
                "openai",
            ],
            check=True,
        )
        version = subprocess.run(
            [
                str(bindir / "python"),
                "-c",
                "import importlib.metadata as m; print(m.version('inferrail'))",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        label = f"{version}+{_git_label()}" if local else version

        upstream_port = _free_port()
        upstream = ThreadingHTTPServer(("127.0.0.1", upstream_port), _StandIn)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()

        (work / "inferrail.yaml").write_text(CONFIG_YAML.format(upstream_port=upstream_port))
        (work / "agent_run.py").write_text(AGENT_RUN)
        (work / "contract.txt").write_text(CONTRACT)

        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        env.update(
            PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
            XDG_DATA_HOME=str(tmp / "data"),
            STAND_IN_KEY="unused-by-the-stand-in",
            INFERRAIL_TELEMETRY="0",
            PYTHONUNBUFFERED="1",
        )

        def run(cmd: str) -> str:
            return subprocess.run(
                cmd.split(), cwd=work, env=env, capture_output=True, text=True, check=True
            ).stdout

        show_out = run(SHOW_CMD)
        serve_log = tmp / "serve.log"
        with serve_log.open("w") as log:
            server = subprocess.Popen(
                SERVE_CMD.split(), cwd=work, env=env, stdout=log, stderr=subprocess.STDOUT
            )
        try:
            _wait_for(f"http://127.0.0.1:{PORT}/health")
            serve_out = serve_log.read_text()
            token = re.search(r"dashboard/\?token=(\S+)", serve_out).group(1)  # type: ignore[union-attr]
            dashboard = f"http://127.0.0.1:{PORT}/dashboard/?token={token}"
            shots: list[dict[str, Any]] = []

            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(
                    viewport={"width": 600, "height": 1000}, device_scale_factor=2
                )

                def shot(
                    scene: str,
                    top_sel: str,
                    bottom_sel: str,
                    height: float = 0,
                    right_sel: str = "",
                ) -> None:
                    """Screenshot the region from one element's top to another's
                    bottom (or a fixed height), across the content column, or
                    only as far right as `right_sel` reaches."""
                    top = page.locator(top_sel).first.bounding_box()
                    bottom = page.locator(bottom_sel).last.bounding_box()
                    assert top and bottom
                    pad = 14
                    x, y = top["x"] - pad, top["y"] - pad
                    w = max(top["width"], bottom["width"]) + 2 * pad
                    if right_sel:
                        right = page.locator(right_sel).last.bounding_box()
                        assert right
                        w = max(top["width"], right["x"] + right["width"] - top["x"]) + 2 * pad
                    h = height or (bottom["y"] + bottom["height"] - top["y"] + 2 * pad)
                    name = f"{len(shots):02d}-{scene}.png"
                    page.screenshot(
                        path=str(SHOTS / name), clip={"x": x, "y": y, "width": w, "height": h}
                    )
                    shots.append({"file": name, "scene": scene})

                def goto(route: str, ready: str, page: Page = page) -> None:
                    page.goto(f"{dashboard}{route}")
                    page.wait_for_selector(ready)
                    page.wait_for_timeout(500)

                goto("", "text=Connected")
                # Run the agent while the Live Feed is open; the feed polls once
                # a second, so keep watching until every call's receipt shows.
                agent = subprocess.Popen(
                    [str(bindir / "python"), "agent_run.py"],
                    cwd=work,
                    env=env,
                    stdout=subprocess.PIPE,
                    text=True,
                )
                rows = page.locator(".receipt-row")
                feed_h = 0.0
                seen, expected, agent_out = 0, None, ""
                deadline = time.time() + 60
                while expected is None or seen < expected:
                    if time.time() > deadline:
                        sys.exit(f"Live Feed showed {seen} of {expected} receipts")
                    if expected is None and agent.poll() is not None:
                        agent_out = agent.stdout.read() if agent.stdout else ""
                        if agent.returncode != 0:
                            sys.exit(f"agent_run.py failed:\n{agent_out}")
                        expected = len(agent_out.splitlines())
                    if rows.count() > seen:
                        seen = rows.count()
                        page.wait_for_timeout(120)
                        if not any(s["scene"] == "dashboard" for s in shots):
                            # Establishing shot: the whole app at normal framing,
                            # as the first receipt lands; then back to close crops.
                            page.set_viewport_size({"width": 960, "height": 600})
                            page.wait_for_timeout(150)
                            name = f"{len(shots):02d}-dashboard.png"
                            page.screenshot(path=str(SHOTS / name))
                            shots.append({"file": name, "scene": "dashboard"})
                            page.set_viewport_size({"width": 600, "height": 1000})
                            page.wait_for_timeout(150)
                            continue
                        if not feed_h:
                            # Fixed crop: just the rows, with room for all six.
                            row = rows.first.bounding_box()
                            assert row
                            feed_h = 6 * row["height"] + 28
                        shot("feed", ".receipt-list", ".receipt-list", height=feed_h)
                    page.wait_for_timeout(60)

                goto(f"#/work/{WORK_ID}", "text=Blocked by budget")
                shot("work", "h2.screen-title", ".work-hero", right_sel=".work-hero .figure")
                goto("#/budgets", ".budget-card .budget-note")
                page.wait_for_selector(".receipt-row.status-error")
                shot("budgets", ".budget-card", ".receipt-list")
                browser.close()

            receipts = str(tmp / "data" / "inferrail" / "receipts.db")
            evidence = {
                "work.txt": run(f"inferrail work {WORK_ID} --receipts {receipts}"),
                "report-by-work.txt": run(f"inferrail report --by work_id --receipts {receipts}"),
            }
        finally:
            server.terminate()
            server.wait(timeout=10)
            upstream.shutdown()

        def mask(text: str) -> str:
            return text.replace(token, "<token>").replace(str(tmp), "<tmp>")

        for stale in ("pip.txt", "report-by-customer.txt"):
            (CAPTURE / stale).unlink(missing_ok=True)
        (CAPTURE / "serve.txt").write_text(mask(serve_log.read_text()))
        (CAPTURE / "agent_run.py").write_text(AGENT_RUN)
        (CAPTURE / "show.txt").write_text(show_out)
        (CAPTURE / "agent_run.txt").write_text(agent_out)
        (CAPTURE / "inferrail.yaml").write_text(CONFIG_YAML.format(upstream_port="<port>"))
        for name, text in evidence.items():
            (CAPTURE / name).write_text(mask(text))
        meta = {
            "inferrail_version": version,
            "label": label,
            "package": "this checkout" if local else package_spec,
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "api_keys_in_env": False,
            "upstream": "local stand-in (scripts/render_dashboard_gif.py), no provider called",
            "upstream_latency_s": UPSTREAM_LATENCY_S,
            "commands": [SERVE_CMD, SHOW_CMD, RUN_CMD],
            "screens": shots,
        }
        (CAPTURE / "capture.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"captured inferrail {label} to {CAPTURE.relative_to(REPO)} ({len(shots)} screens)")


# --- rendering -------------------------------------------------------------

WIDTH, BODY_H, STRIP_H = 960, 600, 44
FONT_SIZE, LINE_H, PAD_X, PAD_TOP = 21, 31, 28, 28

BG = (11, 22, 34)
FG = (214, 222, 230)
DIM = (127, 140, 153)
PROMPT = (43, 196, 217)
OK = (126, 211, 146)
BAD = (240, 128, 112)
PAPER = (239, 239, 233)  # the dashboard's own background (--paper)
STRIP_BG = (20, 32, 27)  # the dashboard's --ink
STRIP_FG = (239, 239, 233)
STRIP_DIM = (139, 151, 142)


@dataclass
class Frame:
    caption: str
    hold_ms: int
    lines: list[str] | None = None
    screen: str | None = None
    fit_to: str | None = None  # place `screen` at this screen's scale and origin


def _font(size: int, bold: bool = False) -> Any:
    from PIL import ImageFont

    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    for base in ("/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "C:/Windows/Fonts"):
        path = Path(base) / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.truetype(name, size)


def _draw(frame: Frame, label: str) -> Any:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, STRIP_H + BODY_H), BG if frame.lines is not None else PAPER)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH, STRIP_H], fill=STRIP_BG)
    d.text((PAD_X, 12), frame.caption, font=_font(17, bold=True), fill=STRIP_FG)
    tag = f"inferrail {label} · stand-in model, no key"
    small = _font(11)
    d.text((WIDTH - PAD_X - small.getlength(tag), 17), tag, font=small, fill=STRIP_DIM)

    if frame.screen is not None:
        # Fit the crop to the frame, keeping its aspect; any margin is the
        # dashboard's own paper background.
        shot = Image.open(SHOTS / frame.screen).convert("RGB")
        ref = Image.open(SHOTS / (frame.fit_to or frame.screen))
        scale = min(WIDTH / ref.width, BODY_H / ref.height)
        origin = (
            (WIDTH - round(ref.width * scale)) // 2,
            STRIP_H + (BODY_H - round(ref.height * scale)) // 2,
        )
        size = (round(shot.width * scale), round(shot.height * scale))
        img.paste(shot.resize(size, Image.Resampling.LANCZOS), origin)
        return img

    regular, bold = _font(FONT_SIZE), _font(FONT_SIZE, bold=True)
    char_w = regular.getlength("M")
    for i, line in enumerate(frame.lines or []):
        y = STRIP_H + PAD_TOP + i * LINE_H
        if line.startswith("$ "):
            d.text((PAD_X, y), "$", font=bold, fill=PROMPT)
            d.text((PAD_X + char_w * 2, y), line[2:], font=bold, fill=FG)
        elif line.rstrip().endswith("answered"):
            d.text((PAD_X, y), line, font=regular, fill=OK)
        elif " blocked (" in line:
            d.text((PAD_X, y), line, font=regular, fill=BAD)
        else:
            d.text((PAD_X, y), line, font=regular, fill=FG if line.strip() else DIM)
    return img


def _typing(caption: str, prefix: list[str], cmd: str, steps: int = 3) -> list[Frame]:
    return [
        Frame(caption, 60, lines=prefix + [f"$ {cmd[: round(len(cmd) * s / steps)]}"])
        for s in range(1, steps)
    ]


def _scenes(meta: dict[str, Any]) -> list[Frame]:
    serve = (CAPTURE / "serve.txt").read_text().splitlines()
    dash = next(line for line in serve if line.startswith("Dashboard:"))
    shown = (CAPTURE / "show.txt").read_text().rstrip("\n").splitlines()
    ran = (CAPTURE / "agent_run.txt").read_text().rstrip("\n").splitlines()

    frames: list[Frame] = []
    cap = "A real local run: start Inferrail and its dashboard"
    frames += _typing(cap, [], SERVE_CMD)
    started = [f"$ {SERVE_CMD}", dash]
    frames.append(Frame(cap, 1000, lines=started))

    cap = "Give one AI job a $0.04 spending limit"
    frames += _typing(cap, [], SHOW_CMD)
    tagged = [f"$ {SHOW_CMD}", *shown, ""]
    frames.append(Frame(cap, 1700, lines=tagged))

    cap = "Run the job: it tries six model calls"
    frames += _typing(cap, tagged, RUN_CMD)
    frames.append(Frame(cap, 2200, lines=[*tagged, f"$ {RUN_CMD}", *ran]))

    screens = meta["screens"]
    dashboard = next(s for s in screens if s["scene"] == "dashboard")
    frames.append(Frame("The local Inferrail dashboard", 1700, screen=dashboard["file"]))
    feed = [s for s in screens if s["scene"] == "feed"]
    cap = "Each call is recorded with what it cost"
    for i, s in enumerate(feed):
        frames.append(Frame(cap, 900 if i == len(feed) - 1 else 300, screen=s["file"]))

    # The work and budget frames share one scale and origin, so the money
    # figures stay put and the blocked details appear beneath them.
    work = next(s for s in screens if s["scene"] == "work")
    budgets = next(s for s in screens if s["scene"] == "budgets")
    frames.append(Frame("What the job spent", 2600, screen=work["file"], fit_to=budgets["file"]))

    cap = "Limit reached: the rest were blocked before the model"
    frames.append(Frame(cap, 4800, screen=budgets["file"]))
    return frames


def render() -> None:
    from PIL import Image

    meta = json.loads((CAPTURE / "capture.json").read_text())
    frames = _scenes(meta)
    images = [_draw(f, meta["label"]) for f in frames]
    # A palette per frame keeps both the terminal colors and the
    # dashboard's paper tones exact.
    quantized = [
        im.quantize(colors=64, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
        for im in images
    ]
    quantized[0].save(
        GIF_PATH,
        save_all=True,
        append_images=quantized[1:],
        duration=[f.hold_ms for f in frames],
        loop=0,
        optimize=True,
        disposal=1,
    )
    images[-1].save(POSTER_PATH, optimize=True)
    total_s = sum(f.hold_ms for f in frames) / 1000
    size_kb = GIF_PATH.stat().st_size / 1024
    print(
        f"wrote {GIF_PATH.relative_to(REPO)}: "
        f"{len(frames)} frames, {total_s:.1f}s, {size_kb:.0f} KB"
    )
    print(f"wrote {POSTER_PATH.relative_to(REPO)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--capture", action="store_true", help="re-run the product before rendering"
    )
    parser.add_argument(
        "--package",
        default=".",
        help="pip requirement to capture (default: this checkout; "
        "'inferrail' captures the latest PyPI release)",
    )
    args = parser.parse_args()
    if args.capture:
        capture(args.package)
    render()


if __name__ == "__main__":
    main()
