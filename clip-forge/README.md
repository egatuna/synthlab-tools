# clip-forge

Turn a long video (local file or YouTube URL) into vertical **9:16 short-form
clips** with burned-in, word-synced captions. An Opus Clip replacement built on
faster-whisper + an LLM judgment call + ffmpeg.

## Usage

```bash
/opt/anaconda3/bin/python3 clip-forge/clipforge.py run <video.mp4 | youtube-url> [--mock-llm] [--keep-work]
```

- `--mock-llm` — skip the (paid) LLM call and fabricate 2 candidate clips from
  the transcript timespan. For offline pipeline testing only.
- `--keep-work` — keep the `work/` scratch dir (downloads, `.ass` files) instead
  of deleting it after the run.

### Pipeline

1. **Input** — a URL is downloaded (best mp4 ≤1080p, via yt-dlp) into `work/`;
   a local path is used directly.
2. **Transcribe** — faster-whisper (`whisper_model`, word-level timestamps).
3. **Select** — the timestamped transcript is sent to the Fable-tier judgment
   model, which returns 3–8 candidate clips with honest `hook_score`s.
4. **Render** — every clip scoring `>= min_hook_score` is cut, center-cropped to
   1080×1920 (focal offset configurable), captioned, and re-encoded (h264 + aac,
   faststart) in a single ffmpeg pass so caption sync stays exact.
5. **Manifest** — results land in
   `output/<video-stem>-<YYYYMMDD-HHMM>/` as `clip-01.mp4 …` + `manifest.json`.

If **no** clip clears the threshold, no clips are written — but a `manifest.json`
is still produced with an explanatory `note` (never junk clips).

## Config knobs (`config.yaml`)

| key | default | meaning |
|-----|---------|---------|
| `whisper_model` | `base` | faster-whisper model size |
| `min_hook_score` | `7` | clips below this are skipped, not rendered |
| `clip_min_s` / `clip_max_s` | `15` / `60` | allowed clip length (seconds) |
| `focal_offset` | `0.0` | 9:16 crop horizontal focus, −1 = left … 0 = center … 1 = right |
| `caption.font` / `.size` / `.outline` / `.margin_v` / `.words_per_line` | Arial / 64 / 4 / 420 / 4 | burned-in caption style (ASS, 1080×1920 canvas; `margin_v` puts text ~78% down) |
| `output_dir` / `work_dir` | `output` / `work` | filesystem paths (relative to `clip-forge/`) |

## Feeding the GTA 6 Shorts pipeline — the `manifest.json` contract

Each run writes one `manifest.json`; the Shorts pipeline reads it as the
hand-off contract:

```json
{
  "source": "/abs/path/to/source.mp4",
  "created": "2026-07-04T14:42:00",
  "clips": [
    {
      "file": "clip-01.mp4",
      "start_s": 12.0,
      "end_s": 34.5,
      "hook_score": 8,
      "title": "…",
      "hashtags": ["gta6", "shorts"]
    }
  ],
  "skipped": [ { "start_s": …, "end_s": …, "hook_score": 4, "title": "…" } ],
  "note": "1 clip(s) generated at hook_score >= 7."
}
```

- `clips[]` — the render-ready shorts. `file` is relative to the manifest dir.
- `skipped[]` — below-threshold candidates (kept for review/tuning).
- `note` — always present; explains empty runs so the downstream pipeline can log
  "no strong moments" instead of publishing filler.

Downstream just iterates `clips[]`, joins `file` against the manifest's
directory, and schedules each `clip-NN.mp4` with its `title` / `hashtags`.

## Offline testing

Transcription is injectable so the pipeline can run without speech audio or any
paid call:

```bash
export SYNTHLAB_CLIPFORGE_FAKE_TRANSCRIPT=clip-forge/tests/fake_transcript.json
/opt/anaconda3/bin/python3 clip-forge/clipforge.py run /path/test.mp4 --mock-llm --keep-work
```

`SYNTHLAB_CLIPFORGE_FAKE_TRANSCRIPT` loads `{segments, words}` from JSON instead
of running whisper. Combined with `--mock-llm`, the whole crop → caption →
manifest path is exercised deterministically and for free.
