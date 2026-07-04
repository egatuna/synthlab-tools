#!/usr/bin/env python3
"""clip-forge — turn a long video (file or YouTube URL) into vertical short-form
clips with burned-in word-synced captions. An Opus Clip replacement.

Pipeline (see README):
  1. resolve input  (download URL -> mp4, or use local file)
  2. transcribe     (faster-whisper, word-level timestamps)
  3. select         (LLM picks 3-8 candidate clips + honest hook scores)
  4. render         (ffmpeg 9:16 center-crop + burned-in .ass captions)
  5. manifest       (output/<stem>-<stamp>/manifest.json for the Shorts pipeline)

Usage:
  clipforge.py run <video.mp4 | youtube-url> [--mock-llm] [--keep-work]

Never calls the LLM under --mock-llm (fabricates candidates from the transcript
timespan) so the full pipeline can be exercised offline and for free.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# --- repo bootstrap: put repo root on sys.path so `shared` imports resolve ----
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from shared import llm, notify  # noqa: E402

TOOL = "clip-forge"
HERE = Path(__file__).resolve().parent
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

DEFAULT_CONFIG: dict[str, Any] = {
    "whisper_model": "base",
    "min_hook_score": 7,
    "clip_min_s": 15,
    "clip_max_s": 60,
    "focal_offset": 0.0,
    "caption": {
        "font": "Arial",
        "size": 64,
        "outline": 4,
        "margin_v": 420,
        "words_per_line": 4,
    },
    "output_dir": "output",
    "work_dir": "work",
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict[str, Any]:
    """Load config.yaml and merge it over the built-in defaults."""
    cfg: dict[str, Any] = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    if path.exists():
        import yaml

        loaded = yaml.safe_load(path.read_text()) or {}
        for key, value in loaded.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    return cfg


# --------------------------------------------------------------------------- #
# Stage 1 — input resolution
# --------------------------------------------------------------------------- #
def is_url(arg: str) -> bool:
    """True if the CLI argument looks like an http(s) URL."""
    return arg.startswith("http://") or arg.startswith("https://")


def download_video(url: str, work_dir: Path) -> Path:
    """Download the best mp4 <=1080p from a URL via the yt_dlp Python API."""
    import yt_dlp

    outtmpl = str(work_dir / "%(id)s.%(ext)s")
    opts = {
        "format": (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "best[height<=1080][ext=mp4]/best[height<=1080]/best"
        ),
        "outtmpl": outtmpl,
        "merge_output_format": "mp4",
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        raw = Path(ydl.prepare_filename(info))
    merged = raw.with_suffix(".mp4")
    return merged if merged.exists() else raw


def resolve_input(arg: str, work_dir: Path) -> Path:
    """Return a local mp4 path for `arg`, downloading it first if it is a URL."""
    if is_url(arg):
        notify.log(TOOL, f"downloading {arg}")
        path = download_video(arg, work_dir)
        notify.log(TOOL, f"downloaded -> {path.name}")
        return path
    path = Path(arg).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"input file not found: {path}")
    return path


# --------------------------------------------------------------------------- #
# Stage 2 — transcription
# --------------------------------------------------------------------------- #
def transcribe(video: Path, model_size: str) -> tuple[list[dict], list[dict]]:
    """Return (segments, words) with word-level timestamps.

    Injectable for offline testing: if SYNTHLAB_CLIPFORGE_FAKE_TRANSCRIPT points
    at a JSON file of {"segments": [...], "words": [...]}, that is loaded instead
    of running whisper. Each segment is {start, end, text}; each word is
    {start, end, word}.
    """
    fake = os.environ.get("SYNTHLAB_CLIPFORGE_FAKE_TRANSCRIPT")
    if fake:
        notify.log(TOOL, f"using injected transcript {fake}")
        data = json.loads(Path(fake).read_text())
        return data.get("segments", []), data.get("words", [])

    from faster_whisper import WhisperModel

    notify.log(TOOL, f"transcribing with faster-whisper ({model_size})")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    seg_iter, _info = model.transcribe(str(video), word_timestamps=True)

    segments: list[dict] = []
    words: list[dict] = []
    for seg in seg_iter:
        segments.append(
            {"start": float(seg.start), "end": float(seg.end), "text": seg.text.strip()}
        )
        for w in seg.words or []:
            words.append(
                {"start": float(w.start), "end": float(w.end), "word": w.word}
            )
    notify.log(TOOL, f"transcript: {len(segments)} segments, {len(words)} words")
    return segments, words


def compact_transcript(segments: list[dict]) -> str:
    """Render segments as `[mm:ss] text` lines for the LLM prompt."""
    lines = []
    for s in segments:
        minutes, seconds = divmod(int(s["start"]), 60)
        lines.append(f"[{minutes:02d}:{seconds:02d}] {s['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Stage 3 — segment selection
# --------------------------------------------------------------------------- #
_SELECT_SYSTEM = (
    "You are a short-form video editor. You find the moments in a long video "
    "that would perform as standalone TikTok / Reels / YouTube Shorts clips. "
    "You score hooks honestly: a genuinely gripping opening is a 9-10, a solid "
    "moment is 6-7, and a boring or context-dependent stretch is a 2-4. Never "
    "inflate a dull clip to clear a threshold."
)


def _select_prompt(transcript: str, cfg: dict) -> str:
    return (
        "Below is a timestamped transcript of a long video, one line per "
        "sentence/segment as `[mm:ss] text`.\n\n"
        "Pick 3 to 8 candidate short-form clips. For each clip return an object "
        "with these exact keys:\n"
        '  "start_s": float seconds, "end_s": float seconds,\n'
        '  "hook_score": integer 1-10, "title": short punchy string,\n'
        '  "hashtags": array of 3-6 lowercase hashtag strings (no # sign),\n'
        '  "why": one sentence on why it works as a clip.\n\n'
        "Hard constraints:\n"
        f"  - Each clip must be between {cfg['clip_min_s']} and "
        f"{cfg['clip_max_s']} seconds long.\n"
        "  - start_s and end_s must land on natural sentence boundaries "
        "(use the segment timestamps as the boundaries).\n"
        "  - hook_score must be honest per the scoring guide.\n\n"
        "Return a JSON array only.\n\n"
        f"TRANSCRIPT:\n{transcript}"
    )


def _normalize_candidate(item: dict, cfg: dict, span: tuple[float, float]) -> dict | None:
    """Validate/clamp one LLM candidate. Returns None if unusable."""
    try:
        start = float(item["start_s"])
        end = float(item["end_s"])
        score = int(round(float(item["hook_score"])))
    except (KeyError, TypeError, ValueError):
        return None

    lo, hi = span
    start = max(lo, min(start, hi))
    end = max(lo, min(end, hi))
    if end <= start:
        return None
    # clamp duration into [clip_min_s, clip_max_s]
    dur = end - start
    if dur > cfg["clip_max_s"]:
        end = start + cfg["clip_max_s"]
    if end - start < cfg["clip_min_s"]:
        end = min(hi, start + cfg["clip_min_s"])
    if end - start < 1.0:
        return None

    hashtags = item.get("hashtags") or []
    if not isinstance(hashtags, list):
        hashtags = [str(hashtags)]
    return {
        "start_s": round(start, 3),
        "end_s": round(end, 3),
        "hook_score": max(1, min(10, score)),
        "title": str(item.get("title", "")).strip() or "Untitled clip",
        "hashtags": [str(h).lstrip("#").strip() for h in hashtags if str(h).strip()],
        "why": str(item.get("why", "")).strip(),
    }


def _mock_segments(segments: list[dict], cfg: dict) -> list[dict]:
    """Fabricate 2 candidate clips from the transcript timespan (no LLM call).

    Used for offline pipeline testing. Returns [] when the transcript is empty
    so the no-clips manifest path can be exercised too.
    """
    if not segments:
        return []
    t0 = float(segments[0]["start"])
    t1 = float(segments[-1]["end"])
    if t1 - t0 <= 0:
        return []

    cmin = float(cfg["clip_min_s"])
    cmax = float(cfg["clip_max_s"])
    boundaries = [t0] + [float(s["end"]) for s in segments]

    def snap(target: float) -> float:
        return min(boundaries, key=lambda b: abs(b - target))

    mid = snap(t0 + (t1 - t0) / 2)
    raw = [(t0, mid), (mid, t1)]

    out: list[dict] = []
    for idx, (start, end) in enumerate(raw, 1):
        dur = end - start
        if dur > cmax:
            end = start + cmax
        if end - start < cmin:
            end = min(t1, start + cmin)
        if end - start < 1.0:
            continue
        out.append(
            {
                "start_s": round(start, 3),
                "end_s": round(end, 3),
                # high enough to clear the default threshold so clips render
                "hook_score": 8,
                "title": f"Mock clip {idx}",
                "hashtags": ["shorts", "clip", "test"],
                "why": "Fabricated by --mock-llm for offline pipeline testing.",
            }
        )
    return out


def select_segments(
    segments: list[dict], cfg: dict, *, mock: bool
) -> list[dict]:
    """Return validated candidate clips, sorted by hook_score descending."""
    if mock:
        candidates = _mock_segments(segments, cfg)
        notify.log(TOOL, f"--mock-llm fabricated {len(candidates)} candidate(s)")
        return sorted(candidates, key=lambda c: c["hook_score"], reverse=True)

    if not segments:
        return []

    span = (float(segments[0]["start"]), float(segments[-1]["end"]))
    prompt = _select_prompt(compact_transcript(segments), cfg)
    raw = llm.complete_json(prompt, system=_SELECT_SYSTEM, judgment=True)
    if isinstance(raw, dict):
        # tolerate {"clips": [...]}
        raw = raw.get("clips") or raw.get("segments") or []
    if not isinstance(raw, list):
        raise ValueError(f"LLM returned non-list candidates: {type(raw).__name__}")

    candidates = [c for c in (_normalize_candidate(i, cfg, span) for i in raw) if c]
    notify.log(TOOL, f"LLM proposed {len(raw)} clip(s), {len(candidates)} valid")
    return sorted(candidates, key=lambda c: c["hook_score"], reverse=True)


# --------------------------------------------------------------------------- #
# Stage 4 — captions (.ass) + render (ffmpeg)
# --------------------------------------------------------------------------- #
def build_caption_lines(
    words: list[dict], seg_start: float, seg_end: float, cfg: dict
) -> list[tuple[float, float, str]]:
    """Group in-window words into caption lines, times re-based to clip start.

    Each line runs first-word start -> last-word end, clamped to >=300ms and
    de-overlapped. Returns (start, end, TEXT) tuples in clip-local seconds.
    """
    per_line = int(cfg["caption"].get("words_per_line", 4))
    min_dur = 0.3

    in_window = [
        w for w in words if float(w["end"]) > seg_start and float(w["start"]) < seg_end
    ]
    groups: list[list[dict]] = []
    for w in in_window:
        if not groups or len(groups[-1]) >= per_line:
            groups.append([])
        groups[-1].append(w)

    lines: list[tuple[float, float, str]] = []
    for g in groups:
        start = max(0.0, float(g[0]["start"]) - seg_start)
        end = max(0.0, float(g[-1]["end"]) - seg_start)
        if end - start < min_dur:
            end = start + min_dur
        text = " ".join(str(w["word"]).strip() for w in g).strip().upper()
        if text:
            lines.append((start, end, text))

    # de-overlap: trim a line's end back to the next line's start
    for i in range(len(lines) - 1):
        s, e, t = lines[i]
        next_start = lines[i + 1][0]
        if e > next_start:
            lines[i] = (s, max(s + 0.05, next_start), t)
    return lines


_CANVAS = (1080, 1920)


def _load_font(name: str, size: int):
    """Load a bold TrueType font for `name`, falling back to Arial Bold."""
    from PIL import ImageFont

    candidates = [
        f"/System/Library/Fonts/Supplemental/{name} Bold.ttf",
        f"/System/Library/Fonts/Supplemental/{name}.ttf",
        f"/Library/Fonts/{name} Bold.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _wrap_text(draw, text: str, font, max_w: int, stroke: int) -> str:
    """Greedy word-wrap `text` to fit `max_w`, returning newline-joined lines."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = (current + " " + word).strip()
        bbox = draw.textbbox((0, 0), trial, font=font, stroke_width=stroke)
        if bbox[2] - bbox[0] > max_w and current:
            lines.append(current)
            current = word
        else:
            current = trial
    if current:
        lines.append(current)
    return "\n".join(lines)


def render_caption_png(text: str, cfg: dict, out_path: Path) -> None:
    """Render one caption line to a transparent 1080x1920 PNG.

    White bold text with a thick black outline, centered horizontally and placed
    ~78% down the canvas (via caption.margin_v). Overlaid onto the video by
    ffmpeg so the caption is genuinely burned in.
    """
    from PIL import Image, ImageDraw

    c = cfg["caption"]
    w, h = _CANVAS
    stroke = int(c["outline"])
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _load_font(str(c["font"]), int(c["size"]))

    wrapped = _wrap_text(draw, text, font, int(w * 0.9), stroke)
    bbox = draw.multiline_textbbox(
        (0, 0), wrapped, font=font, stroke_width=stroke, align="center", spacing=8
    )
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (w - text_w) // 2 - bbox[0]
    y = h - int(c["margin_v"]) - text_h - bbox[1]
    draw.multiline_text(
        (x, y),
        wrapped,
        font=font,
        fill=(255, 255, 255, 255),
        stroke_width=stroke,
        stroke_fill=(0, 0, 0, 255),
        align="center",
        spacing=8,
    )
    img.save(out_path)


def _crop_scale_vf(cfg: dict) -> str:
    """The 9:16 center-crop + scale filter, focal_offset applied to crop x."""
    off = float(cfg["focal_offset"])
    return (
        "crop=w=ih*9/16:h=ih:"
        f"x=(iw-ih*9/16)*(1+({off}))/2:y=0,"
        "scale=1080:1920,setsar=1"
    )


def render_clip(
    source: Path,
    seg: dict,
    out_path: Path,
    captions: list[tuple[float, float, Path]],
    cfg: dict,
) -> None:
    """Cut `seg` from `source`, crop to 9:16 1080x1920, burn captions, encode.

    Captions are pre-rendered PNGs composited with the `overlay` filter, each
    gated to its word-timed window via `enable='between(...)'`. Times are
    clip-local (the input is seeked to seg.start_s) so sync stays exact.
    (This build's ffmpeg lacks the libass `subtitles` filter; overlay is the
    portable burn path — see README.)
    """
    duration = seg["end_s"] - seg["start_s"]
    cmd = [
        FFMPEG,
        "-y",
        "-ss",
        f"{seg['start_s']}",
        "-t",
        f"{duration}",
        "-i",
        str(source),
    ]
    for _s, _e, png in captions:
        cmd += ["-i", str(png)]

    if captions:
        parts = [f"[0:v]{_crop_scale_vf(cfg)}[v0]"]
        prev = "v0"
        for i, (start, end, _png) in enumerate(captions):
            nxt = f"v{i + 1}"
            parts.append(
                f"[{prev}][{i + 1}:v]overlay=0:0:"
                f"enable='between(t,{start:.3f},{end:.3f})'[{nxt}]"
            )
            prev = nxt
        cmd += ["-filter_complex", ";".join(parts), "-map", f"[{prev}]", "-map", "0:a?"]
    else:
        cmd += ["-vf", _crop_scale_vf(cfg), "-map", "0:v", "-map", "0:a?"]

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {out_path.name}: {result.stderr.strip()[-800:]}"
        )


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
def run(arg: str, *, mock_llm: bool, keep_work: bool, cfg: dict) -> Path:
    """Execute the full pipeline. Returns the output directory."""
    work_dir = (HERE / cfg["work_dir"]).resolve()
    out_root = (HERE / cfg["output_dir"]).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    try:
        # 1. input
        source = resolve_input(arg, work_dir)
        stem = source.stem

        # 2. transcribe
        segments, words = transcribe(source, cfg["whisper_model"])

        # 3. select
        candidates = select_segments(segments, cfg, mock=mock_llm)
        threshold = int(cfg["min_hook_score"])
        selected = [c for c in candidates if c["hook_score"] >= threshold]
        skipped = [c for c in candidates if c["hook_score"] < threshold]

        # 4. output dir
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        out_dir = out_root / f"{stem}-{stamp}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 5. render each selected clip
        clips: list[dict] = []
        for idx, seg in enumerate(selected, 1):
            clip_name = f"clip-{idx:02d}.mp4"
            out_path = out_dir / clip_name
            lines = build_caption_lines(words, seg["start_s"], seg["end_s"], cfg)
            captions: list[tuple[float, float, Path]] = []
            for j, (start, end, text) in enumerate(lines):
                png = work_dir / f"clip-{idx:02d}-cap-{j:02d}.png"
                render_caption_png(text, cfg, png)
                captions.append((start, end, png))
            notify.log(
                TOOL,
                f"rendering {clip_name} "
                f"[{seg['start_s']:.1f}-{seg['end_s']:.1f}s] "
                f"score={seg['hook_score']} lines={len(lines)}",
            )
            render_clip(source, seg, out_path, captions, cfg)
            clips.append(
                {
                    "file": clip_name,
                    "start_s": seg["start_s"],
                    "end_s": seg["end_s"],
                    "hook_score": seg["hook_score"],
                    "title": seg["title"],
                    "hashtags": seg["hashtags"],
                }
            )

        # 6. explanatory note (PRD: never emit junk clips)
        if not segments:
            note = "No transcript / no speech detected; no clips generated."
        elif not candidates:
            note = "No candidate moments were identified; no clips generated."
        elif not selected:
            note = (
                f"{len(candidates)} candidate(s) found but none met "
                f"hook_score >= {threshold}; no clips generated (see skipped)."
            )
        else:
            note = f"{len(clips)} clip(s) generated at hook_score >= {threshold}."

        manifest = {
            "source": str(source),
            "created": datetime.now().isoformat(),
            "clips": clips,
            "skipped": skipped,
            "note": note,
        }
        (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
        notify.log(TOOL, f"done: {note} -> {out_dir}")
        return out_dir
    finally:
        if not keep_work and work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="clipforge", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="process a video into short-form clips")
    run_p.add_argument("input", help="local video file or a YouTube/URL")
    run_p.add_argument(
        "--mock-llm",
        action="store_true",
        help="skip the LLM; fabricate 2 candidates (offline testing)",
    )
    run_p.add_argument(
        "--keep-work",
        action="store_true",
        help="keep the work/ scratch dir instead of cleaning it",
    )
    args = parser.parse_args(argv)

    cfg = load_config(HERE / "config.yaml")
    try:
        out_dir = run(
            args.input, mock_llm=args.mock_llm, keep_work=args.keep_work, cfg=cfg
        )
    except Exception as exc:  # never-silent failure
        notify.fail(TOOL, f"run failed: {exc}", detail=repr(exc))
        print(f"clip-forge: FAILED — {exc}", file=sys.stderr)
        return 1
    print(f"clip-forge: wrote {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
