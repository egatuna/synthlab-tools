# post-batcher

A Buffer replacement for SynthLab Co. Generates a week of platform-tailored
social posts, keeps them in a human-editable markdown queue, and serves today's
unposted posts as copy-ready blocks or an 8am email draft.

## Usage

```bash
PY=/opt/anaconda3/bin/python3

# Generate this week's queue from a brief (writes queue/<YYYY-Www>.md)
$PY postbatcher.py plan "Launch of the AI site-audit offer" --week 2026-W27

# Offline, no LLM call (deterministic filler — for smoke tests)
$PY postbatcher.py plan "test brief" --mock-llm --force

# Print today's UNPOSTED posts as copy-ready blocks (platform + char count)
$PY postbatcher.py today

# 8am cron entry: email today's unposted posts, or exit quietly if none
$PY postbatcher.py digest

# Mark posts done (flips [ ] -> [x]); one platform or all for a date
$PY postbatcher.py mark-posted 2026-07-06 X
$PY postbatcher.py mark-posted 2026-07-06
```

## Queue format

`queue/<YYYY-Www>.md` — strictly parseable but hand-editable:

```
# Post Queue — 2026-W27
## 2026-07-06 (Mon)
### X
- [ ] <post text>
### LinkedIn
- [ ] <first line>
  <continuation lines indented two spaces>
### TikTok
- [ ] <caption #tags>
```

`- [ ]` = unposted, `- [x]` = posted. The parser tolerates hand edits: extra
blank lines, reordered days, missing platforms, and edited text all survive.
`plan` refuses to overwrite an existing week file unless `--force`.

## Length rules (validated in code, not just prompted)

- **X** — hard 280 char ceiling.
- **LinkedIn** — ~1300 visible chars, line-broken, no hashtag spam.
- **TikTok** — caption <= 150 chars plus 3-5 hashtags.

After generation every post is length-checked. Violators trigger one LLM retry
that regenerates only the offenders; anything still over is truncated at a word
boundary with `…` and a warning is logged. Limits live in `config.yaml`.

## Cron

- `digest` at 08:00 daily — drafts the email, silent when nothing is due.
