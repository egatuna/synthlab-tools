# booking-bridge — replaces Calendly

Public booking page: **https://form.jotform.com/261846157248061**
(Jotform form `261846157248061`, "Book a call with SynthLab Co")

## How it works

- The form's "Preferred time slot" dropdown only ever shows real, future,
  conflict-free 30-minute slots.
- `flows/slots-refresh.md` (Mondays 6:00) reads the next 14 days of Google
  Calendar, regenerates the dropdown options, and stores the
  label → ISO-datetime `slot_map` in the flow state.
- `flows/booking-poll.md` (every 30 min) picks up new submissions,
  conflict-checks the requested slot against the calendar at booking time,
  then either creates the event (requester as guest) + a confirmation Gmail
  draft, or — if the slot was taken in a race — drafts an apology with the
  3 nearest free alternatives. Submission IDs are recorded in state after
  each booking, so a submission is never processed twice.

Both flows are executed by the automation-runner dispatcher; no separate
cron entries are needed beyond the dispatcher's 5-minute tick.

## Config

`config.yaml` — working hours/days, timezone, slot length, lookahead,
minimum notice, event title template. Edit and the next slots-refresh run
picks it up.

## Notes

- Drafts, not sends: confirmations and apologies land in Gmail drafts for a
  human glance before sending (flip the flow wording to send directly once
  trusted).
- End-to-end latency: submission → calendar event within 30 minutes (the
  poll cadence), per the PRD acceptance criteria.
