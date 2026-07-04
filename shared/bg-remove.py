#!/usr/bin/env python3
"""Background removal for a folder of images (replaces Canva Pro bg-remove).

Usage:
    python3 shared/bg-remove.py <input-dir-or-file> [output-dir]

Writes <name>-nobg.png next to each input (or into output-dir).
"""

import sys
from pathlib import Path

from rembg import remove
from PIL import Image

EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def process(path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{path.stem}-nobg.png"
    with Image.open(path) as img:
        remove(img).save(out)
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    src = Path(sys.argv[1]).expanduser()
    files = [src] if src.is_file() else sorted(
        p for p in src.iterdir() if p.suffix.lower() in EXTS)
    if not files:
        print(f"no images found in {src}")
        return 1
    out_dir = Path(sys.argv[2]).expanduser() if len(sys.argv) > 2 else \
        (src.parent if src.is_file() else src)
    for f in files:
        print(f"✓ {process(f, out_dir)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
