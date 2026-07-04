# contact-hunter

A Hunter/Apollo-lite email finder. Feed it a markdown lead file from the
lead-prospector and it returns the same file with a verified `Email` and a
`Confidence` rating for each business.

## Usage

```bash
python hunter.py run leads.md            # writes leads-enriched.md
python hunter.py run leads.md --out found.md
python hunter.py run leads.md --config config.yaml
```

## What it does

1. **Parses** the lead file tolerantly — markdown tables *or* per-business
   `## Heading` sections. Pulls name, website, Facebook URL and owner name.
2. **Scrapes** the homepage + contact pages (`/contact`, `/about`, …) and the
   Facebook page (best effort). Reads `mailto:` links and de-obfuscates
   `name (at) domain (dot) com` / `name[at]domain[dot]com`. Junk (asset files,
   `sentry`, `wixpress`, `example.com`, …) is filtered.
3. **Falls back** to pattern candidates (`info@`, `contact@`, `<owner-first>@`)
   when nothing is scraped.
4. **Verifies** every address against the domain's MX records (dnspython).

### Confidence

| Value      | Meaning                                        |
| ---------- | ---------------------------------------------- |
| `confirmed`| scraped from the site **and** domain has MX    |
| `plausible`| pattern-generated **and** domain has MX        |
| `none`     | no MX — **no email is emitted** (hard rule)    |

The input file is never mutated; a copy is written with the two new fields
appended (extra table columns, or `**Email:** … | **Confidence:** …` lines).

## Config (`config.yaml`)

`request_delay_s` (global ≥2s pacing), `timeout_s`, `contact_paths`,
`email_patterns` (local-part prefixes), `junk_domains`.

## Tests (offline)

```bash
/opt/anaconda3/bin/python3 tests/test_offline.py
```

Stubs the network + DNS layer (`fetch_url`, `resolve_mx`) — no real requests,
no LLM. Covers parsing, de-obfuscation, MX suppression and output rendering.
