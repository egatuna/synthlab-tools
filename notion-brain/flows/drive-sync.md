---
name: notion-brain-drive-sync
schedule: "0 2 * * *"
enabled: false  # paused 2026-07-06 — claude.ai connectors unavailable in headless claude -p (github.com/anthropics/claude-code #36587, still broken on 2.1.202, now tracked as #75059); re-enable when a -p session lists mcp__claude_ai_* tools
allowed_tools: [mcp__claude_ai_Google_Drive]
timeout_seconds: 900
---

You are the nightly Drive-sync worker for **notion-brain**. Your job is to
mirror a set of Google Drive folders into a local markdown cache so the Python
indexer can build its FTS5 search index. Work carefully and idempotently.

## Steps

1. Read `notion-brain/config.yaml` and take the `drive_folders` list (folder
   names) and `cache_dir` (default `cache`, relative to `notion-brain/`).

2. For each folder name in `drive_folders`:
   a. Use the Google Drive MCP to locate the folder and list every document
      inside it (Google Docs and text-like files). Recurse into subfolders if
      present.
   b. For each document, get its metadata: the file id (`source_id`), name
      (`source_name`), and `modifiedTime` (`modified_time`).
   c. Determine the target cache path:
      `notion-brain/<cache_dir>/<sanitized-folder>/<sanitized-title>.md`
      where sanitizing means: lowercase is NOT required, but replace `/`,
      backslashes, and other path-hostile characters with `-`, collapse
      whitespace to single spaces, and trim. Keep names stable across runs so
      the same doc always maps to the same file.
   d. If a cache file already exists at that path, read its frontmatter and
      compare the stored `modified_time` with Drive's current `modified_time`.
      **Only re-download and rewrite the file if the modified_time changed**
      (or the file is missing). Skip unchanged docs.
   e. When writing, read the doc's text content and write the file as:

      ```
      ---
      source_id: <file id>
      source_name: <original doc title>
      modified_time: <Drive modifiedTime, ISO 8601>
      synced_at: <the current UTC time, ISO 8601>
      ---

      <full plain-text / markdown content of the doc>
      ```

3. **Prune deletions:** after syncing, for every existing file under
   `cache_dir` whose `source_id` no longer corresponds to a live Drive document
   in the configured folders, delete that cache file. The cache must be an exact
   mirror of what currently lives in the Drive folders.

4. You may use the flow's own state file
   `automation-runner/state/notion-brain-drive-sync.json` for bookkeeping
   (e.g. last successful sync time, per-file id→modified_time map) to make the
   next run faster. This is optional; the frontmatter is the source of truth.

## Reply contract

- End your reply with exactly `FLOW_OK` on success (even if nothing changed).
- If the Google Drive MCP is unavailable or you cannot authenticate, do not
  guess or fabricate cache files — end with `FLOW_FAIL: <short reason>`.
