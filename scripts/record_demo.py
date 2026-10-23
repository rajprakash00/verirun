"""Record the short terminal demo embedded in the README.

Runs the real offline demo with its stdout captured, renders the captured
transcript as a terminal page, records the playback with Playwright, and writes
``docs/media/demo.webm`` plus a GIF converted with ffmpeg.

The demo is deterministic and needs no model or API key. Recording needs
Chromium (``uv run playwright install chromium``) and ffmpeg.

Usage: uv run python scripts/record_demo.py
"""

from __future__ import annotations

import html
import io
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

from verirun import demo

ROOT = Path(__file__).resolve().parents[1]
MEDIA = ROOT / "docs" / "media"
VIEWPORT = {"width": 960, "height": 680}
INTRO_HOLD_MS = 900
OUTRO_HOLD_MS = 2600
MIN_DELAY_S = 0.16
MAX_DELAY_S = 0.9

CLASSES = (
    ("  [happy]", "run"),
    ("  [transient]", "run"),
    ("  [approval]", "run"),
    ("           verification:", "ok"),
    ("           recovery:", "warn"),
    ("  parked at", "warn"),
    ("  human decision:", "warn"),
    ("           evidence:", "dim"),
    ("1/3 ", "step"),
    ("2/3 ", "step"),
    ("3/3 ", "step"),
    ("Demo complete.", "ok strong"),
    ("Verirun demo", "title"),
)


class TimestampedWriter(io.TextIOBase):
    """Capture stdout as (line, elapsed seconds) pairs."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, float]] = []
        self._buffer = ""
        self._start = time.monotonic()

    def write(self, text: str) -> int:
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self.lines.append((line, time.monotonic() - self._start))
        return len(text)

    def finish(self) -> list[tuple[str, float]]:
        if self._buffer:
            self.lines.append((self._buffer, time.monotonic() - self._start))
            self._buffer = ""
        return self.lines


def capture() -> list[tuple[str, float]]:
    writer = TimestampedWriter()
    argv = [
        "--runs",
        "runs",
        "--state-dir",
        "mocks/state",
        "--shared",
        "shared",
        "--no-browser",
    ]
    with redirect_stdout(writer):
        code = demo.main(argv)
    if code != 0:
        raise SystemExit(f"the demo failed with exit code {code}")
    return writer.finish()


def classify(line: str) -> str:
    for prefix, css in CLASSES:
        if line.startswith(prefix):
            return css
    return ""


def delays(lines: list[tuple[str, float]]) -> list[int]:
    result: list[int] = []
    previous = 0.0
    for _, at in lines:
        gap = min(max(at - previous, MIN_DELAY_S), MAX_DELAY_S)
        result.append(int(gap * 1000))
        previous = at
    return result


def render(lines: list[tuple[str, float]]) -> str:
    rows = []
    for line, _ in lines:
        text = html.escape(line) if line else "&nbsp;"
        rows.append(f'<div class="line reveal {classify(line)}">{text}</div>')
    schedule = ", ".join(str(delay) for delay in delays(lines))
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>verirun demo</title>
<style>
  * {{ box-sizing: border-box; }}
  html, body {{ height: 100%; margin: 0; }}
  body {{
    background: #05070b; color: #d7dde8;
    font: 14px/1.45 "DejaVu Sans Mono", ui-monospace, SFMono-Regular, Menlo, monospace;
    display: grid; place-items: center;
  }}
  .window {{
    width: 920px; height: 640px; background: #0e1117;
    border: 1px solid #222b3a; border-radius: 12px; overflow: hidden;
    box-shadow: 0 24px 80px rgba(0, 0, 0, .55);
  }}
  .bar {{
    height: 38px; background: #151b25; border-bottom: 1px solid #222b3a;
    display: flex; align-items: center; gap: 8px; padding: 0 16px;
  }}
  .dot {{ width: 11px; height: 11px; border-radius: 50%; }}
  .red {{ background: #ff5f57; }} .amber {{ background: #febc2e; }} .green {{ background: #28c840; }}
  .titlebar {{ margin-left: 10px; color: #7f8ca3; font-size: 12px; }}
  .screen {{ padding: 18px 22px; height: calc(100% - 38px); overflow: hidden; }}
  .line {{ white-space: pre-wrap; opacity: 0; transition: opacity .08s linear; }}
  .line.visible {{ opacity: 1; }}
  .prompt {{ color: #61d6a5; }}
  .path {{ color: #6aa5ff; }}
  .run {{ color: #e7ecf5; font-weight: 700; }}
  .ok {{ color: #5fd38d; }}
  .warn {{ color: #f3c969; }}
  .dim {{ color: #7f8ca3; }}
  .step {{ color: #8ab4ff; font-weight: 700; }}
  .title {{ color: #e7ecf5; font-weight: 700; }}
  .strong {{ font-weight: 700; }}
  .cursor {{ color: #61d6a5; animation: blink 1s steps(1) infinite; }}
  @keyframes blink {{ 50% {{ opacity: 0; }} }}
</style>
</head>
<body>
  <div class="window">
    <div class="bar">
      <span class="dot red"></span><span class="dot amber"></span><span class="dot green"></span>
      <span class="titlebar">verirun demo — invoice processing</span>
    </div>
    <div class="screen">
      <div class="line run visible"><span class="prompt">~/verirun</span> <span class="path">$</span> uv run verirun demo</div>
      {''.join(rows)}
      <span class="cursor">▍</span>
    </div>
  </div>
<script>
  const lines = Array.from(document.querySelectorAll('.line.reveal'));
  const schedule = [{schedule}];
  let index = 0;
  function step() {{
    if (index >= lines.length) {{
      window.__done = true;
      return;
    }}
    lines[index].classList.add('visible');
    const delay = schedule[index] || 160;
    index += 1;
    setTimeout(step, delay);
  }}
  setTimeout(step, {INTRO_HOLD_MS});
</script>
</body>
</html>
"""


def record(page_html: Path, out_webm: Path) -> None:
    from playwright.sync_api import sync_playwright

    with tempfile.TemporaryDirectory() as tmp, sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(
            viewport=VIEWPORT,
            record_video_dir=tmp,
            record_video_size=VIEWPORT,
        )
        page = context.new_page()
        page.goto(page_html.as_uri())
        page.wait_for_function("window.__done === true", timeout=120_000)
        page.wait_for_timeout(OUTRO_HOLD_MS)
        video = page.video
        context.close()
        browser.close()
        if video is None:
            raise SystemExit("Playwright did not record a video")
        shutil.copyfile(video.path(), out_webm)


def make_gif(webm: Path, gif: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg is required to write the GIF; install it and retry")
    filters = (
        "fps=10,scale=800:-1:flags=lanczos,"
        "split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=bayer"
    )
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(webm),
            "-vf",
            filters,
            "-loop",
            "0",
            str(gif),
        ],
        check=True,
    )


def main() -> int:
    if not (Path.cwd() / "company").is_dir():
        print("error: run this from the repository root", file=sys.stderr)
        return 1
    MEDIA.mkdir(parents=True, exist_ok=True)
    lines = capture()
    with tempfile.TemporaryDirectory() as tmp:
        page_html = Path(tmp) / "demo.html"
        page_html.write_text(render(lines), encoding="utf-8")
        webm = MEDIA / "demo.webm"
        record(page_html, webm)
    make_gif(webm, MEDIA / "demo.gif")
    print(f"wrote {MEDIA / 'demo.webm'} and {MEDIA / 'demo.gif'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
