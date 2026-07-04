---
name: morning-brief
schedule: "0 7 * * *"
enabled: true
allowed_tools: [mcp__claude_ai_Gmail, mcp__claude_ai_Google_Calendar, mcp__claude_ai_Stripe]
timeout_seconds: 600
---
GOAL: Compile one concise "Morning Brief" digest for today and leave it as a Gmail
DRAFT to yourself. This flow keeps no state — each day is fresh.

No state file is needed. Do NOT read or write automation-runner/state — this flow
starts clean every day.

Build a single digest with these three sections. If any section's data source is
unavailable/unauthenticated, SKIP that section gracefully with a one-line note
("Calendar unavailable", etc.) and continue — a missing source is NOT a failure.

SECTION 1 — Inbox.
- Search Gmail for unread threads in the inbox. Summarize the top ~5 by
  importance: for each give sender, one-line subject/topic, and an urgency read
  (urgent / normal / FYI). If inbox is clean, say "Inbox clear".

SECTION 2 — Today's calendar.
- List today's Google Calendar events in chronological order. For each: start–end
  time and title (plus location or video link if present). If no events, say
  "No events scheduled today".

SECTION 3 — Stripe (last 24h).
- Summarize Stripe activity in the last 24 hours: successful payments (count +
  total amount), plus any refunds or disputes. If Stripe is unavailable, note it
  and skip. If there was no activity, say "No Stripe activity in the last 24h".

DELIVER.
- Create a Gmail DRAFT (do NOT send) addressed to yourself. Subject exactly:
  "Morning Brief — <today's date, e.g. Jul 4, 2026>". Body = the three sections
  above, clean and skimmable (headers + short bullets). Keep it tight.

Even if every section is empty or unavailable, still create the draft and end
successfully.

Finish with exactly FLOW_OK on success or FLOW_FAIL: <reason> if you could not
create the draft at all (e.g. Gmail unavailable).
