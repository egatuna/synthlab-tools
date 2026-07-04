#!/usr/bin/env python3
"""Batch resize/crop images to platform presets (replaces Canva Pro resize).

Usage:
    python3 shared/img-resize.py <preset> <input-dir-or-file> [output-dir]

Presets:
    ig-square   1080x1080     ig-story   1080x1920    ig-portrait 1080x1350
    yt-thumb    1280x720      x-post     1600x900     li-post     1200x627
    etsy        2000x1600     og         1200x630     shorts      1080x1920

Center-crops to the preset aspect ratio, then resizes. Output: JPEG q90
(<name>-<preset>.jpg) into output-dir (default: alongside input).
"""

import sys
from pathlib import Path

from PIL import Image

PRESETS = {
    "ig-square": (1080, 1080), "ig-story": (1080, 1920), "ig-portrait": (1080, 1350),
    "yt-thumb": (1280, 720), "x-post": (1600, 900), "li-post": (1200, 627),
    "etsy": (2000, 1600), "og": (1200, 630), "shorts": (1080, 1920),
}
EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def fit(img: Image.Image, w: int, h: int) -> Image.Image:
    target = w / h
    cur = img.width / img.height
    if cur > target:  # too wide — crop sides
        new_w = int(img.height * target)
        x = (img.width - new_w) // 2
        img = img.crop((x, 0, x + new_w, img.height))
    elif cur < target:  # too tall — crop top/bottom
        new_h = int(img.width / target)
        y = (img.height - new_h) // 2
        img = img.crop((0, y, img.width, y + new_h))
    return img.resize((w, h), Image.LANCZOS)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in PRESETS:
        print(__doc__)
        return 1
    preset, src = sys.argv[1], Path(sys.argv[2]).expanduser()
    w, h = PRESETS[preset]
    files = [src] if src.is_file() else sorted(
        p for p in src.iterdir() if p.suffix.lower() in EXTS)
    if not files:
        print(f"no images found in {src}")
        return 1
    out_dir = Path(sys.argv[3]).expanduser() if len(sys.argv) > 3 else \
        (src.parent if src.is_file() else src)
    out_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        with Image.open(f) as img:
            out = out_dir / f"{f.stem}-{preset}.jpg"
            fit(img.convert("RGB"), w, h).save(out, "JPEG", quality=90)
            print(f"✓ {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
