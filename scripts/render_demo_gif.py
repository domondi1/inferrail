"""Capture a real `inferrail demo` run and render it as the README GIF.

Two steps, both reproducible:

1. ``--capture`` runs the installed ``inferrail`` CLI in a fresh temporary
   directory with outbound network blocked inside the child process
   (``connect``/``getaddrinfo`` for IP sockets raise and are counted), then
   writes the exact stdout of each command, the receipts the demo wrote,
   and a small ``capture.json`` (version, Python, network attempts) to
   ``docs/assets/demo-capture/``.
2. Rendering reads only those captured files and draws terminal frames
   with Pillow. Nothing shown in the GIF is typed by hand: every output
   line comes from the capture. Scenes show excerpts of that output.

Usage (from a checkout, with ``inferrail`` and Pillow installed)::

    python -m pip install pillow
    python scripts/render_demo_gif.py --capture   # re-run the demo, then render
    python scripts/render_demo_gif.py             # re-render from the committed capture

Outputs: ``docs/assets/inferrail-demo.gif`` and
``docs/assets/inferrail-demo-poster.png`` (a static frame for readers who
prefer no animation). Pillow is a dev-time tool here, not a package
dependency.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
ASSETS = REPO / "docs" / "assets"
CAPTURE = ASSETS / "demo-capture"
GIF_PATH = ASSETS / "inferrail-demo.gif"
POSTER_PATH = ASSETS / "inferrail-demo-poster.png"

DEMO_CMD = "inferrail demo"
REPORT_CMD = "inferrail report --by customer --receipts ./inferrail-demo-receipts.jsonl"
PRICED_CMD = "head -n 1 inferrail-demo-receipts.jsonl | python -m json.tool --no-ensure-ascii"
UNKNOWN_CMD = (
    "grep demo-preview inferrail-demo-receipts.jsonl | python -m json.tool --no-ensure-ascii"
)

BANNER = "SYNTHETIC DEMO DATA  |  no API key  |  network blocked  |  not provider billing"

# Loaded into the child process via PYTHONPATH. Blocks and records any
# attempt to open an IP connection or resolve a hostname.
_NETWORK_GUARD = '''
import os, socket
_LOG = os.environ["INFERRAIL_DEMO_NET_LOG"]
open(_LOG, "a").close()
def _record(what):
    with open(_LOG, "a") as f:
        f.write(what + "\\n")
_orig_connect = socket.socket.connect
def _connect(self, address):
    if self.family in (socket.AF_INET, socket.AF_INET6):
        _record("connect %r" % (address,))
        raise OSError("network blocked by render_demo_gif.py")
    return _orig_connect(self, address)
socket.socket.connect = _connect
def _getaddrinfo(*args, **kwargs):
    _record("getaddrinfo %r" % (args[:2],))
    raise OSError("DNS blocked by render_demo_gif.py")
socket.getaddrinfo = _getaddrinfo
'''


def _run(cmd: str, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(
        cmd, shell=True, cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout


def capture() -> None:
    inferrail = shutil.which("inferrail")
    if inferrail is None:
        sys.exit("`inferrail` is not on PATH; install it first (python -m pip install inferrail)")
    CAPTURE.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp_str:
        tmp = Path(tmp_str)
        guard_dir = tmp / "guard"
        guard_dir.mkdir()
        (guard_dir / "sitecustomize.py").write_text(_NETWORK_GUARD)
        work = tmp / "work"
        work.mkdir()
        net_log = tmp / "net.log"

        env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
        env["PYTHONPATH"] = str(guard_dir)
        env["INFERRAIL_DEMO_NET_LOG"] = str(net_log)
        env["COLUMNS"] = "100"
        python = sys.executable

        outputs = {
            "demo.txt": _run(DEMO_CMD, work, env),
            "report.txt": _run(REPORT_CMD, work, env),
            "receipt-priced.txt": _run(PRICED_CMD.replace("python", python, 1), work, env),
            "receipt-unknown.txt": _run(UNKNOWN_CMD.replace("python", python, 1), work, env),
        }
        if not net_log.exists():
            sys.exit("network guard did not load in the child process; capture is not valid")
        attempts = [line for line in net_log.read_text().splitlines() if line]

        for name, text in outputs.items():
            (CAPTURE / name).write_text(text)
        receipts = "inferrail-demo-receipts.jsonl"
        shutil.copy(work / receipts, CAPTURE / receipts)
        version = _run(
            f'"{python}" -c "import importlib.metadata as m; print(m.version(\'inferrail\'))"',
            work,
            env,
        ).strip()
        meta = {
            "inferrail_version": version,
            "python": platform.python_version(),
            "platform": platform.system().lower(),
            "network_attempts": len(attempts),
            "network_attempt_log": attempts,
            "api_keys_in_env": False,
            "commands": [DEMO_CMD, REPORT_CMD, PRICED_CMD, UNKNOWN_CMD],
        }
        (CAPTURE / "capture.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(f"captured to {CAPTURE.relative_to(REPO)} ({len(attempts)} network attempts)")


# --- rendering -------------------------------------------------------------

COLS = 92
FONT_SIZE = 16
LINE_H = 21
PAD_X = 22
PAD_TOP = 58
ROWS = 27

BG = (11, 22, 34)
FG = (214, 222, 230)
DIM = (127, 140, 153)
PROMPT = (43, 196, 217)
BANNER_BG = (2, 26, 54)
BANNER_FG = (167, 232, 240)
HILITE_BG = (59, 47, 20)
HILITE_FG = (245, 198, 107)


@dataclass
class Frame:
    lines: list[str]
    hold_ms: int
    highlight: set[int] = field(default_factory=set)


def _font(bold: bool = False) -> Any:
    from PIL import ImageFont

    names = ["DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"]
    for base in ("/usr/share/fonts/truetype/dejavu", "/Library/Fonts", "C:/Windows/Fonts"):
        for name in names:
            path = Path(base) / name
            if path.exists():
                return ImageFont.truetype(str(path), FONT_SIZE)
    return ImageFont.truetype(names[0], FONT_SIZE)


def _draw(frame: Frame, fonts: tuple[Any, Any]) -> Any:
    from PIL import Image, ImageDraw

    regular, bold = fonts
    char_w = regular.getlength("M")
    width = int(PAD_X * 2 + char_w * COLS)
    height = PAD_TOP + LINE_H * ROWS + 16
    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, 38], fill=BANNER_BG)
    d.text((PAD_X, 10), BANNER, font=bold, fill=BANNER_FG)
    visible = frame.lines[-ROWS:]
    offset = len(frame.lines) - len(visible)
    for i, line in enumerate(visible):
        y = PAD_TOP + i * LINE_H
        text = line[:COLS]
        if (i + offset) in frame.highlight:
            d.rectangle([PAD_X - 6, y - 2, width - PAD_X + 6, y + LINE_H - 3], fill=HILITE_BG)
            d.text((PAD_X, y), text, font=regular, fill=HILITE_FG)
        elif text.startswith("$ "):
            d.text((PAD_X, y), "$", font=bold, fill=PROMPT)
            d.text((PAD_X + char_w * 2, y), text[2:], font=bold, fill=FG)
        else:
            d.text((PAD_X, y), text, font=regular, fill=FG if text.strip() else DIM)
    return img


def _typing(prefix: list[str], cmd: str, steps: int = 6) -> list[Frame]:
    frames = []
    for s in range(1, steps + 1):
        n = round(len(cmd) * s / steps)
        frames.append(Frame(prefix + [f"$ {cmd[:n]}"], 90))
    frames[-1].hold_ms = 350
    return frames


def _lines(name: str) -> list[str]:
    return (CAPTURE / name).read_text().rstrip("\n").splitlines()


def _scenes() -> list[Frame]:
    demo = _lines("demo.txt")
    end = next(i for i, line in enumerate(demo) if line.startswith("TOTAL")) + 1
    demo_part = demo[:end]
    report = _lines("report.txt")
    priced = _lines("receipt-priced.txt")
    unknown = _lines("receipt-unknown.txt")

    frames: list[Frame] = []
    # 1. inferrail demo: requests, tokens, then the per-customer cost table.
    frames += _typing([], DEMO_CMD)
    requests_end = next(i for i, line in enumerate(demo_part) if line.startswith("Done.")) - 1
    frames.append(Frame([f"$ {DEMO_CMD}"] + demo_part[:requests_end], 2200))
    frames.append(Frame([f"$ {DEMO_CMD}"] + demo_part, 3600))

    # 2. The follow-up report reads the demo ledger; the unknown-cost row is marked.
    frames += _typing([], REPORT_CMD)
    shown = [f"$ {REPORT_CMD}"] + report
    unknown_rows = {i for i, line in enumerate(shown) if line.rstrip().endswith(" 1")}
    frames.append(Frame(shown, 1600))
    frames.append(Frame(shown, 2600, unknown_rows))

    # 3. One real receipt: usage, price snapshot, cost, attribution. No message text.
    frames += _typing([], PRICED_CMD)
    frames.append(Frame([f"$ {PRICED_CMD}"] + priced, 4200))

    # 4. A model with no price on file: pricing and cost are null, not $0.
    frames += _typing([], UNKNOWN_CMD)
    shown = [f"$ {UNKNOWN_CMD}"] + unknown
    nulls = {i for i, line in enumerate(shown) if line.strip().endswith("null,")}
    frames.append(Frame(shown, 1400))
    frames.append(Frame(shown, 4200, nulls))
    return frames


def render() -> None:
    from PIL import Image

    fonts = (_font(), _font(bold=True))
    frames = _scenes()
    images = [_draw(f, fonts) for f in frames]
    palette_src = images[len(images) // 2].quantize(colors=32, method=Image.Quantize.MEDIANCUT)
    quantized = [im.quantize(palette=palette_src, dither=Image.Dither.NONE) for im in images]
    quantized[0].save(
        GIF_PATH,
        save_all=True,
        append_images=quantized[1:],
        duration=[f.hold_ms for f in frames],
        loop=0,
        optimize=True,
        disposal=1,
    )
    # Poster: the completed `inferrail demo` frame (requests plus cost table).
    poster = next(i for i, f in enumerate(frames) if f.hold_ms >= 3000)
    images[poster].save(POSTER_PATH, optimize=True)
    total_s = sum(f.hold_ms for f in frames) / 1000
    size_kb = GIF_PATH.stat().st_size / 1024
    print(
        f"wrote {GIF_PATH.relative_to(REPO)}: "
        f"{len(frames)} frames, {total_s:.1f}s, {size_kb:.0f} KB"
    )
    print(f"wrote {POSTER_PATH.relative_to(REPO)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--capture", action="store_true", help="re-run the demo before rendering")
    args = parser.parse_args()
    if args.capture:
        capture()
    render()


if __name__ == "__main__":
    main()
