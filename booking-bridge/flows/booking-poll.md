> **RETIRED 2026-07-09 (owner directive: Jotform is ENS Staffing-only).**
> This flow depended on a SynthLab form living on the ENS Jotform account —
> that account's email defaults leak the ENS inbox and brand, and no SynthLab
> form may exist there. It was already paused by the MCP headless-connector
> regression (claude-code#75059) and is now retired outright. If booking/intake
> comes back, rebuild it on Netlify Forms (see
> synthlab-games/gift-pipeline/intake/ for the working pattern) or a
> SynthLab-owned account. Do not re-enable as-is.

---
name: booking-poll
schedule: "*/30 * * * *"
enabled: false  # paused 2026-07-06 — claude.ai connectors unavailable in headless claude -p (github.com/anthropics/claude-code #36587, still broken on 2.1.202, now tracked as #75059); re-enable when a -p session lists mcp__claude_ai_* tools
allowed_tools: [mcp__claude_ai_Google_Calendar, mcp__claude_ai_Jotform, mcp__claude_ai_Gmail]
timeout_seconds: 900
---
You process new bookings from the SynthLab Co booking form. Exactly one
calendar event per successful booking; a race for the same slot gets an
apology-with-alternatives draft instead of a double-booking.

1. Read `booking-bridge/config.yaml` (form id, timezone, event settings). If
   `jotform_form_id` is empty, end with `FLOW_FAIL: form id not configured`.
2. Read your state file. It contains `processed_ids` (submission IDs already
   handled) and `slot_map` (dropdown label → ISO start datetime, written by
   the slots-refresh flow).
3. List recent submissions for the form. For each submission whose ID is NOT
   in `processed_ids`, in oldest-first order:
   a. Extract name, email, selected time-slot label, and any message.
   b. Resolve the label to an ISO start via `slot_map`. If the label is
      missing from the map, parse the label itself (format
      `Tue Jul 7 — 10:00 AM (ET)`) in the configured timezone.
   c. CONFLICT CHECK (mandatory, immediately before creating): list calendar
      events overlapping [start, start + duration_minutes]. 
      - If free: create the event on the primary calendar — title from
        `title_template` with the requester's name, the requester's email as
        a guest, description including their message and "Booked via
        SynthLab booking form". Then create a Gmail DRAFT to the requester
        confirming date/time (state the timezone), warm and brief.
      - If occupied (double-book race): do NOT create an event. Compute the
        3 nearest still-free slots from `slot_map` (re-checking each against
        the calendar) and create a Gmail DRAFT to the requester: apologize
        that the slot was taken just before their request and offer the
        alternatives.
   d. Add the submission ID to `processed_ids` and write the state file
      after EACH submission (so a crash mid-run never double-processes).
4. No new submissions is success: end `FLOW_OK`. If Jotform or Calendar tools
   are unavailable, end `FLOW_FAIL: <service> unavailable` without touching
   state.
