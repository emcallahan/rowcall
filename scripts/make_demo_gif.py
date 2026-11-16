"""Render docs/demo.gif from a real rowcall session.

The commands really run (against JSONPlaceholder, a free fake API), and their
real output is drawn into an animated terminal. Rerun after changing output:

    pip install -e ".[demo]"
    python scripts/make_demo_gif.py
"""

from __future__ import annotations

import io
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rowcall.cli import main  # noqa: E402

OUTPUT = ROOT / "docs" / "demo.gif"
EXAMPLE = ROOT / "examples" / "jsonplaceholder"

COLS, ROWS = 84, 27
FONT_SIZE = 15
PAD = 18
BAR = 30  # fake window title bar

BG = (30, 30, 46)
BAR_BG = (24, 24, 37)
FG = (205, 214, 244)
DIM = (127, 132, 156)
GREEN = (166, 227, 161)
YELLOW = (249, 226, 175)
RED = (243, 139, 168)
BLUE = (137, 180, 250)
DOTS = [(243, 139, 168), (249, 226, 175), (166, 227, 161)]

FONT_CANDIDATES = [
    "C:/Windows/Fonts/CascadiaMono.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "/System/Library/Fonts/Menlo.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


def load_font() -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return ImageFont.truetype(path, FONT_SIZE)
    raise SystemExit("No monospace font found; add one to FONT_CANDIDATES.")


def capture(args: list[str]) -> list[str]:
    """Run rowcall for real and return what it printed, including the yes/no prompt."""
    out = io.StringIO()

    def ask(prompt: str) -> str:
        out.write(prompt + "yes\n")
        return "yes"

    main(args, env={"DEMO_TOKEN": "demo-token-123"}, out=out, ask=ask, interactive=True)
    return out.getvalue().splitlines()


def color_for(line: str) -> tuple[int, int, int]:
    text = line.strip()
    if text.startswith("$"):
        return FG
    if "FAILED" in text or text.startswith(("Error", "line ")):
        return RED
    if text.startswith("[") and ": 2" in text:
        return GREEN
    if text.startswith(("Done:", "Checked")):
        return BLUE
    if text.startswith(("Dry run only", "About to send", "Type yes")):
        return YELLOW
    if text.startswith(("Template:", "CSV:", "Pace:", "Results:")):
        return DIM
    return FG


class Terminal:
    def __init__(self, font):
        self.font = font
        box = font.getbbox("M")
        self.cw = box[2] - box[0]
        self.ch = int(FONT_SIZE * 1.45)
        self.width = PAD * 2 + self.cw * COLS
        self.height = BAR + PAD * 2 + self.ch * ROWS
        self.lines: list[str] = []
        self.frames: list[Image.Image] = []
        self.durations: list[int] = []

    def frame(self, ms: int, cursor: bool = True) -> None:
        img = Image.new("RGB", (self.width, self.height), BG)
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, self.width, BAR], fill=BAR_BG)
        for i, dot in enumerate(DOTS):
            x = 16 + i * 20
            d.ellipse([x, 10, x + 11, 21], fill=dot)
        d.text((self.width // 2 - 30, 7), "rowcall", font=self.font, fill=DIM)

        visible = self.lines[-ROWS:]
        for row, line in enumerate(visible):
            y = BAR + PAD + row * self.ch
            if line.startswith("$ "):
                d.text((PAD, y), "$", font=self.font, fill=GREEN)
                d.text((PAD + self.cw * 2, y), line[2:], font=self.font, fill=FG)
            else:
                d.text((PAD, y), line, font=self.font, fill=color_for(line))
        if cursor and visible:
            row = len(visible) - 1
            x = PAD + self.cw * len(visible[-1])
            y = BAR + PAD + row * self.ch
            d.rectangle([x, y + 2, x + self.cw - 2, y + self.ch - 4], fill=FG)
        self.frames.append(img)
        self.durations.append(ms)

    def type_command(self, command: str) -> None:
        self.lines.append("$ ")
        self.frame(500)
        for i in range(0, len(command), 3):
            self.lines[-1] = "$ " + command[: i + 3]
            self.frame(35)
        self.frame(400)

    def print_lines(self, lines: list[str], ms: int = 60) -> None:
        for line in lines:
            if line.startswith("Type yes to continue: "):
                self.lines.append("Type yes to continue: ")
                self.frame(900)
                for i in range(1, 4):
                    self.lines[-1] = "Type yes to continue: " + "yes"[:i]
                    self.frame(120)
                self.frame(400)
            else:
                self.lines.append(line[:COLS])
                pause = 220 if line.strip().startswith("[") else ms
                self.frame(pause, cursor=False)

    def clear(self) -> None:
        self.lines = []

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # One shared palette, built from a frame that shows every color, keeps
        # colors stable from frame to frame (no flicker or color bleed).
        busiest = max(self.frames, key=lambda f: len(f.getcolors(1 << 16) or []))
        shared = busiest.quantize(colors=64)
        palette = [f.quantize(palette=shared, dither=Image.Dither.NONE) for f in self.frames]
        palette[0].save(
            path,
            save_all=True,
            append_images=palette[1:],
            duration=self.durations,
            loop=0,
            optimize=True,
        )


def main_demo() -> None:
    work = Path(tempfile.mkdtemp(prefix="rowcall-demo-"))
    try:
        for name in ("posts.csv", "posts-with-problems.csv", "create-post.yaml"):
            shutil.copy(EXAMPLE / name, work / name)
        os.chdir(work)

        scenes = [
            "rowcall run posts-with-problems.csv -t create-post.yaml --dry-run",
            "rowcall run posts.csv -t create-post.yaml --dry-run --show 1",
            "rowcall run posts.csv -t create-post.yaml -o results.csv",
        ]
        term = Terminal(load_font())
        for i, command in enumerate(scenes):
            output = capture(command.split()[1:])
            if i:
                term.clear()
            term.type_command(command)
            term.print_lines(output)
            term.lines.append("$ ")
            term.frame(3500 if i < len(scenes) - 1 else 5000)
        os.chdir(ROOT)
        term.save(OUTPUT)
        size_kb = OUTPUT.stat().st_size // 1024
        print(f"Wrote {OUTPUT.relative_to(ROOT)} ({len(term.frames)} frames, {size_kb} KB)")
    finally:
        os.chdir(ROOT)
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main_demo()
