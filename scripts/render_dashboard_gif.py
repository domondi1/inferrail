"""Capture a real gateway + dashboard run and render it as the README GIF.

Two steps, both reproducible:

1. ``--capture`` installs the published ``inferrail`` package (plus the
   ``openai`` SDK) into a fresh virtual environment, starts
   ``inferrail serve --app-mode`` from it with an isolated data directory,
   runs a small agent script that tags six model calls with one customer,
   one ``work_id`` and a per-run budget, and screenshots the real local
   dashboard with Playwright while those receipts arrive. The model
   upstream is a local stand-in speaking the OpenAI chat-completions wire
   format, so no API key is used and no provider is billed. Everything
   else (gateway, budgets, receipts, dashboard) is the installed package.
   Command output, screenshots and a ``capture.json`` land in
   ``docs/assets/dashboard-capture/``.
2. Rendering reads only those captured files and composes frames with
   Pillow. Terminal text comes from the captured stdout (the local API
   token and temporary paths are masked); browser frames are the
   screenshots. The only additions are a caption strip and a ring marking
   where a real click happened.

Usage (from a checkout; needs network for the install step)::

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
INSTALL_CMD = "pip install inferrail"
SERVE_CMD = "inferrail serve --app-mode --config inferrail.yaml"
SHOW_CMD = "cat agent_run.py"
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

client = OpenAI(base_url="http://127.0.0.1:{PORT}/v1", api_key="unused", default_headers={{
    "X-Inferrail-Attribute-Customer": "acme",
    "X-Inferrail-Attribute-Work-Id": "{WORK_ID}",
    "X-Inferrail-Budget-Usd": "0.04",
}})
contract = open("contract.txt").read()

for step in ["extract clauses", "check liability", "check renewal",
             "compare to playbook", "draft summary", "draft reply"]:
    try:
        client.chat.completions.create(model="gpt-4o", max_tokens=400, messages=[
            {{"role": "user", "content": f"{{step}}:\\n{{contract}}"}}])
        print(f"{{step:<20}} answered")
    except APIStatusError as e:
        print(f"{{step:<20}} refused {{e.status_code}}, provider not called")
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


def capture(package_spec: str) -> None:
    from playwright.sync_api import sync_playwright

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", PORT)) == 0:
            sys.exit(f"port {PORT} is in use; stop whatever is listening there first")
    SHOTS.mkdir(parents=True, exist_ok=True)
    for old in SHOTS.glob("*.png"):
        old.unlink()

    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        venv = tmp / "venv"
        work = tmp / "work"
        work.mkdir()
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        bindir = venv / ("Scripts" if os.name == "nt" else "bin")
        pip = subprocess.run(
            [
                str(bindir / "python"),
                "-m",
                "pip",
                "install",
                "--no-cache-dir",
                package_spec,
                "openai",
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        installed = next(
            line for line in pip.splitlines() if line.startswith("Successfully installed")
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
                    viewport={"width": 768, "height": 512}, device_scale_factor=1.25
                )

                def shot(scene: str, click: Any = None) -> None:
                    name = f"{len(shots):02d}-{scene}.png"
                    page.screenshot(path=str(SHOTS / name))
                    entry: dict[str, Any] = {"file": name, "scene": scene}
                    if click is not None:
                        box = click.bounding_box()
                        entry["click"] = [
                            round((box["x"] + box["width"] / 2) * 1.25),
                            round((box["y"] + box["height"] / 2) * 1.25),
                        ]
                    shots.append(entry)

                page.goto(dashboard)
                page.wait_for_selector("text=CONNECTED")
                page.wait_for_timeout(400)
                shot("feed")
                # Run the agent while the Live Feed is open; screenshot each new row.
                agent = subprocess.Popen(
                    [str(bindir / "python"), "agent_run.py"],
                    cwd=work,
                    env=env,
                    stdout=subprocess.PIPE,
                    text=True,
                )
                # The feed polls once a second, so keep watching after the
                # agent exits until every call's receipt is on screen.
                rows = page.locator("text=work:" + WORK_ID)
                seen, expected, deadline = 0, None, time.time() + 60
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
                        shot("feed")
                    page.wait_for_timeout(60)

                work_tab = page.get_by_role("link", name="Work", exact=True)
                if work_tab.count() == 0:
                    work_tab = page.get_by_text("Work", exact=True).first
                shot("feed", click=work_tab)
                work_tab.click()
                page.wait_for_selector("text=" + WORK_ID)
                page.wait_for_timeout(400)
                row = page.get_by_text(WORK_ID, exact=True).first
                shot("work")
                shot("work", click=row)
                row.click()
                page.wait_for_selector("text=Receipts")
                page.wait_for_timeout(400)
                shot("detail")
                budgets_tab = page.get_by_text("Budgets", exact=True).first
                shot("detail", click=budgets_tab)
                budgets_tab.click()
                page.wait_for_selector("text=work_id:" + WORK_ID)
                page.wait_for_timeout(400)
                shot("budgets")
                browser.close()

            data = tmp / "data" / "inferrail"
            evidence_cmds = {
                "report-by-customer.txt": [
                    "inferrail",
                    "report",
                    "--by",
                    "customer",
                    "--receipts",
                    str(data / "receipts.db"),
                ],
                "work.txt": ["inferrail", "work", WORK_ID, "--receipts", str(data / "receipts.db")],
            }
            evidence = {
                name: subprocess.run(
                    cmd, cwd=work, env=env, capture_output=True, text=True, check=True
                ).stdout
                for name, cmd in evidence_cmds.items()
            }
        finally:
            server.terminate()
            server.wait(timeout=10)
            upstream.shutdown()

        def mask(text: str) -> str:
            return text.replace(token, "<token>").replace(str(tmp), "<tmp>")

        (CAPTURE / "pip.txt").write_text(installed + "\n")
        (CAPTURE / "serve.txt").write_text(mask(serve_log.read_text()))
        (CAPTURE / "agent_run.py").write_text(AGENT_RUN)
        (CAPTURE / "agent_run.txt").write_text(agent_out)
        (CAPTURE / "inferrail.yaml").write_text(CONFIG_YAML.format(upstream_port="<port>"))
        for name, text in evidence.items():
            (CAPTURE / name).write_text(mask(text))
        meta = {
            "inferrail_version": version,
            "package_spec": package_spec,
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "api_keys_in_env": False,
            "upstream": "local stand-in (scripts/render_dashboard_gif.py), no provider called",
            "upstream_latency_s": UPSTREAM_LATENCY_S,
            "commands": [INSTALL_CMD, SERVE_CMD, RUN_CMD],
            "screens": shots,
        }
        (CAPTURE / "capture.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"captured inferrail {version} to {CAPTURE.relative_to(REPO)} ({len(shots)} screens)")


# --- rendering -------------------------------------------------------------

WIDTH, BODY_H, STRIP_H = 960, 640, 46
FONT_SIZE, LINE_H, PAD_X, PAD_TOP = 17, 24, 26, 22

BG = (11, 22, 34)
FG = (214, 222, 230)
DIM = (127, 140, 153)
PROMPT = (43, 196, 217)
OK = (126, 211, 146)
BAD = (240, 128, 112)
STRIP_BG = (17, 24, 28)
STRIP_FG = (236, 239, 233)
STRIP_DIM = (150, 160, 165)
RING = (214, 80, 60)


@dataclass
class Frame:
    caption: str
    hold_ms: int
    lines: list[str] | None = None
    screen: str | None = None
    click: list[int] | None = None


def _font(size: int, bold: bool = False) -> Any:
    from PIL import ImageFont

    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    for base in ("/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "C:/Windows/Fonts"):
        path = Path(base) / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.truetype(name, size)


def _draw(frame: Frame, version: str) -> Any:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (WIDTH, STRIP_H + BODY_H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, WIDTH, STRIP_H], fill=STRIP_BG)
    d.text((PAD_X, 13), frame.caption, font=_font(17, bold=True), fill=STRIP_FG)
    tag = f"inferrail {version} · stand-in model, no key"
    small = _font(13)
    d.text((WIDTH - PAD_X - small.getlength(tag), 16), tag, font=small, fill=STRIP_DIM)

    if frame.screen is not None:
        shot = Image.open(SHOTS / frame.screen).convert("RGB")
        img.paste(shot.crop((0, 0, WIDTH, BODY_H)), (0, STRIP_H))
        if frame.click:
            x, y = frame.click[0], frame.click[1] + STRIP_H
            for r, w in ((20, 3), (27, 2)):
                d.ellipse([x - r, y - r, x + r, y + r], outline=RING, width=w)
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
        elif "refused" in line:
            d.text((PAD_X, y), line, font=regular, fill=BAD)
        else:
            d.text((PAD_X, y), line, font=regular, fill=FG if line.strip() else DIM)
    return img


def _typing(caption: str, prefix: list[str], cmd: str, steps: int = 5) -> list[Frame]:
    frames = [
        Frame(caption, 70, lines=prefix + [f"$ {cmd[: round(len(cmd) * s / steps)]}"])
        for s in range(1, steps + 1)
    ]
    frames[-1].hold_ms = 300
    return frames


def _scenes(meta: dict[str, Any]) -> list[Frame]:
    installed = (CAPTURE / "pip.txt").read_text().split()
    pkg = next(p for p in installed if p.startswith("inferrail-"))
    serve = (CAPTURE / "serve.txt").read_text().splitlines()
    dash = next(line for line in serve if line.startswith("Dashboard:"))
    live = next(line for line in serve if line.strip().startswith("(receipts land here"))
    script = (CAPTURE / "agent_run.py").read_text().rstrip("\n").splitlines()
    ran = (CAPTURE / "agent_run.txt").read_text().rstrip("\n").splitlines()

    frames: list[Frame] = []
    cap = "1  Install"
    frames += _typing(cap, [], INSTALL_CMD)
    frames.append(Frame(cap, 1100, lines=[f"$ {INSTALL_CMD}", f"Successfully installed {pkg} …"]))

    cap = "2  Start the gateway and dashboard"
    frames += _typing(cap, [], SERVE_CMD)
    frames.append(Frame(cap, 2000, lines=[f"$ {SERVE_CMD}", "…", dash, live]))

    cap = "3  Tag one job and give it a $0.04 budget"
    frames.append(Frame(cap, 3600, lines=[f"$ {SHOW_CMD}", *script]))
    frames += _typing(cap, [], RUN_CMD)
    frames.append(Frame(cap, 2600, lines=[f"$ {RUN_CMD}", *ran]))

    screens = meta["screens"]
    feed = [s for s in screens if s["scene"] == "feed" and "click" not in s]
    cap = "4  Each model call becomes a receipt"
    for i, s in enumerate(feed):
        frames.append(Frame(cap, 1500 if i == len(feed) - 1 else 420, screen=s["file"]))
    for s in screens:
        if s["scene"] == "feed" and "click" in s:
            frames.append(Frame(cap, 650, screen=s["file"], click=s["click"]))

    cap = "5  What that work cost"
    work = [s for s in screens if s["scene"] == "work"]
    frames.append(Frame(cap, 1300, screen=work[0]["file"]))
    frames.append(Frame(cap, 650, screen=work[1]["file"], click=work[1]["click"]))
    detail = [s for s in screens if s["scene"] == "detail"]
    frames.append(Frame(cap, 2600, screen=detail[0]["file"]))
    frames.append(Frame(cap, 650, screen=detail[1]["file"], click=detail[1]["click"]))

    cap = "6  Over budget: refused before the provider"
    budgets = next(s for s in screens if s["scene"] == "budgets")
    frames.append(Frame(cap, 5000, screen=budgets["file"]))
    return frames


def render() -> None:
    from PIL import Image

    meta = json.loads((CAPTURE / "capture.json").read_text())
    frames = _scenes(meta)
    images = [_draw(f, meta["inferrail_version"]) for f in frames]
    # One shared palette built from a terminal frame and a dashboard frame.
    sample = Image.new("RGB", (WIDTH, (STRIP_H + BODY_H) * 2))
    sample.paste(images[len(images) - 1], (0, 0))
    terminal = next(im for im, f in zip(images, frames, strict=True) if f.lines)
    sample.paste(terminal, (0, STRIP_H + BODY_H))
    palette = sample.quantize(colors=64, method=Image.Quantize.MEDIANCUT)
    quantized = [im.quantize(palette=palette, dither=Image.Dither.NONE) for im in images]
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
        default="inferrail",
        help="pip requirement to install for the capture (default: latest from PyPI; "
        "use '.' to capture this checkout)",
    )
    args = parser.parse_args()
    if args.capture:
        capture(args.package)
    render()


if __name__ == "__main__":
    main()
