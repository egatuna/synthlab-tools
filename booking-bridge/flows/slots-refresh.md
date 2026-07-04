---
name: booking-slots-refresh
schedule: "0 6 * * 1"
enabled: true
allowed_tools: [mcp__claude_ai_Google_Calendar, mcp__claude_ai_Jotform]
timeout_seconds: 900
---
You maintain the public booking form for SynthLab Co. Regenerate its time-slot
dropdown so it only offers real, future, conflict-free slots.

1. Read the config at `booking-bridge/config.yaml` in the repo. Note the
   `jotform_form_id`, timezone, working days/hours, slot length, lookahead,
   and `min_notice_hours`. If `jotform_form_id` is empty, end with
   `FLOW_FAIL: form id not configured`.
2. List all Google Calendar events on the primary calendar from now through
   `lookahead_days` days ahead.
3. Generate every candidate slot: each working day in the window, from
   working_hours_start to working_hours_end in slot_minutes increments, in the
   configured timezone. Discard any slot that (a) starts sooner than
   min_notice_hours from now, (b) overlaps any existing calendar event, or
   (c) is in the past. Never offer a slot that violates any of these — a stale
   or past slot on the form is a hard failure of this flow's purpose.
4. Keep at most `max_slots_offered` slots, spread evenly across the available
   days (don't cram the cap into the first two days). Format each option
   exactly like: `Tue Jul 7 — 10:00 AM (ET)` and keep an ISO 8601 start
   datetime mapping.
5. Update the Jotform form (edit_form / api_request on form
   `{jotform_form_id}`): replace the options of the dropdown field named
   "Preferred time slot" with the new list, and store the option→ISO mapping
   in the flow state file (key `slot_map`) so booking-poll can resolve
   selections unambiguously.
6. If there are zero available slots, set the dropdown to a single option
   "No slots currently available — email hello@synthlabco.com" and still
   finish successfully.
7. Log a one-line summary of how many slots were published, then end with
   exactly `FLOW_OK`. If the Jotform or Calendar tools are unavailable, end
   with `FLOW_FAIL: <which service> unavailable`.
