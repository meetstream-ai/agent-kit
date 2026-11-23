"""Build `src/meetstream_agent_kit/data/mascot.json` from MIA's official mascot GIFs (dev only).

Run: uv run --with pillow python scripts/build_mascot.py <folder with idle.gif, thinking.gif, ...>
The GIFs are MeetStream's own, from
https://meetstream-public-images.s3.amazonaws.com/mia-state-gifs/mascot-v1/<state>.gif

The kit itself never needs Pillow: it reads the small pixel grids and PNG written here.
"""

from __future__ import annotations

import base64
import io
import json
import sys
from pathlib import Path

from PIL import Image  # dev-only dependency

WIDTH = 28  # terminal pixels across; half blocks make two pixels per row
STATES = {"idle": 10, "thinking": 6, "speaking": 6, "problem": 1}  # frames kept per state
DARK = 60  # dark pixels connected to the edge are the GIF's black background (and its glow)


def frames(path: Path) -> list[Image.Image]:
    image = Image.open(path)
    out = []
    for index in range(image.n_frames):
        image.seek(index)
        out.append(image.convert("RGBA"))
    return out


def _lum(r: int, g: int, b: int) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def transparent(frame: Image.Image) -> Image.Image:
    """Clear the dark background reachable from the edges; dark parts inside MIA (her face) stay."""
    w, h = frame.size
    pixels = frame.load()
    seen = bytearray(w * h)
    stack = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    while stack:
        x, y = stack.pop()
        if not (0 <= x < w and 0 <= y < h) or seen[y * w + x]:
            continue
        seen[y * w + x] = 1
        r, g, b, _a = pixels[x, y]
        if _lum(r, g, b) >= DARK:
            continue
        pixels[x, y] = (0, 0, 0, 0)
        stack += [(x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)]
    return frame


def bbox(frame: Image.Image) -> tuple[int, int, int, int]:
    """The visible part, ignoring the background."""
    return transparent(frame.copy()).getbbox() or (0, 0, frame.width, frame.height)


def main(folder: Path) -> None:
    all_frames = {state: frames(folder / f"{state}.gif") for state in STATES}
    box = None
    for state_frames in all_frames.values():
        for frame in state_frames[:: max(1, len(state_frames) // 6)]:
            b = bbox(frame)
            box = (
                b
                if box is None
                else (min(box[0], b[0]), min(box[1], b[1]), max(box[2], b[2]), max(box[3], b[3]))
            )
    if box is None:
        raise SystemExit("no mascot pixels found")
    w, h = box[2] - box[0], box[3] - box[1]
    height = round(WIDTH * h / w)
    height += height % 2  # whole half-block rows
    data: dict[str, object] = {
        "width": WIDTH,
        "height": height,
        "source": "mia-state-gifs/mascot-v1",
        "states": {},
    }
    for state, keep in STATES.items():
        state_frames = all_frames[state]
        step = max(1, len(state_frames) // keep)
        picked = state_frames[::step][:keep]
        encoded = []
        for frame in picked:
            small = transparent(frame.crop(box).resize((WIDTH, height), Image.LANCZOS))
            raw = bytearray()
            for y in range(height):
                for x in range(WIDTH):
                    r, g, b, a = small.getpixel((x, y))
                    raw += bytes((r, g, b, 255 if a >= 128 else 0))
            encoded.append(base64.b64encode(bytes(raw)).decode("ascii"))
        data["states"][state] = encoded  # type: ignore[index]
    still = transparent(all_frames["idle"][0].crop(box))
    still.thumbnail((160, 160 * h // w), Image.LANCZOS)
    png = io.BytesIO()
    still.save(png, format="PNG", optimize=True)
    data["png"] = base64.b64encode(png.getvalue()).decode("ascii")
    target = Path(__file__).resolve().parents[1] / "src" / "meetstream_agent_kit" / "data" / "mascot.json"
    target.write_text(json.dumps(data, separators=(",", ":")) + "\n", encoding="utf-8")
    print(
        f"{target}: {WIDTH}x{height} px, png {len(png.getvalue())} bytes, {target.stat().st_size} bytes total"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
