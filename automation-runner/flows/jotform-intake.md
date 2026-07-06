---
name: jotform-intake
schedule: "*/30 * * * *"
enabled: false  # paused 2026-07-06 — claude.ai connectors unavailable in headless claude -p (github.com/anthropics/claude-code issue #36587); re-enable when fixed
allowed_tools: [mcp__claude_ai_Jotform, mcp__claude_ai_Google_Drive, mcp__claude_ai_Gmail]
timeout_seconds: 600
---
GOAL: Log every new Jotform submission to a running Drive tracking doc and draft a
personalized follow-up email to each submitter. One tracking line and at most one
follow-up draft per submission — never duplicate.

STEP 1 — Preconditions.
- Confirm the Jotform MCP tools are available and authenticated with one
  read-only call (list your forms). If Jotform is not connected/authenticated,
  do nothing destructive and end with FLOW_FAIL: jotform mcp unavailable

STEP 2 — Load memory.
- Read automation-runner/state/jotform-intake.json (treat as {"processed_ids": []}
  if absent). Hold the existing processed_ids.

STEP 3 — Gather recent submissions across all forms.
- List your Jotform forms, then list recent submissions for each (most recent
  first; the last few days is plenty). For each submission collect: submission id,
  submission timestamp, form name/title, and the submitter's name, email, and
  their main message/response (the free-text field, if any).

STEP 4 — Process each NEW submission (skip ids already in processed_ids).
For each submission whose id is NOT in processed_ids:
  a. Ensure the tracking doc exists. Search Drive for a document named exactly
     "SynthLab Intake Tracking". If it does not exist, create it with a short
     header line and a column legend (Timestamp | Form | Name | Email | Message).
  b. Append ONE summary line to that doc:
     "<timestamp> | <form name> | <name> | <email> | <message excerpt ~140 chars>".
     Append — never overwrite existing content.
  c. Create a Gmail DRAFT (do NOT send) to the submitter's email. Voice:
     SynthLab Co — warm, prompt, professional. Acknowledge their specific inquiry
     (reference the form and what they asked about), say SynthLab Co will follow
     up, and invite any detail. Subject like
     "Thanks for reaching out to SynthLab Co".
  d. Append this submission id to processed_ids.

STEP 5 — Persist memory.
- Write updated state to automation-runner/state/jotform-intake.json with all
  newly handled ids added to processed_ids. Preserve prior entries.

EDGE CASES.
- No submissions, or all already processed: do nothing, say "nothing new", end
  FLOW_OK.
- Submission missing an email: still append the tracking line, skip the draft,
  and still mark the id processed so it is not retried forever.
- Drafts only — never send.

Finish with exactly FLOW_OK on success or FLOW_FAIL: <reason> if blocked.
