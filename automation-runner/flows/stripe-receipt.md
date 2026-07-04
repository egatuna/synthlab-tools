---
name: stripe-receipt
schedule: "*/30 * * * *"
# Stripe MCP is not yet connected in Claude Code — flip to true once it is,
# otherwise this flow would send a failure alert every 30 minutes.
enabled: false
allowed_tools: [mcp__claude_ai_Stripe, mcp__claude_ai_Google_Drive, mcp__claude_ai_Gmail]
timeout_seconds: 600
---
GOAL: For every recent Stripe payment we have not already handled, file a clean
receipt document in Google Drive and draft a thank-you email to the customer.
This must be perfectly idempotent — a customer must NEVER get two receipts for
the same payment.

STEP 1 — Preconditions.
- Confirm the Stripe MCP tools are available and authenticated by making one
  read-only call (e.g. list a small page of recent charges/payment intents).
- If Stripe is not connected or not authenticated, DO NOTHING ELSE. Do not touch
  Drive or Gmail. End your reply with exactly: FLOW_FAIL: stripe mcp unavailable

STEP 2 — Load memory.
- Read automation-runner/state/stripe-receipt.json (treat as {"processed_ids": []}
  if it does not exist). Keep the existing processed_ids list in hand.

STEP 3 — Fetch recent successful payments.
- List Stripe payments (charges or payment intents) with a status of succeeded
  from the LAST 3 DAYS only. Ignore anything older, and ignore failed, pending,
  refunded, or uncaptured payments.
- For each payment collect: payment id, amount + currency, creation date,
  description / product, and the customer's name and email (from the charge,
  its customer object, or billing details — whichever is present).

STEP 4 — Process each NEW payment (skip any id already in processed_ids).
For each payment whose id is NOT in processed_ids:
  a. Locate the Drive folder at path SynthLab/Receipts/. Search Drive for a
     folder named "Receipts" under a "SynthLab" folder. If the SynthLab or
     Receipts folder does not exist, create the missing folder(s) so the path
     SynthLab/Receipts/ exists.
  b. Create a receipt file in that Receipts folder. Name it
     "Receipt <payment_id> <YYYY-MM-DD>". Body is a clean, professional receipt
     containing: SynthLab Co header, receipt date, payment id, amount (formatted
     with currency, e.g. $250.00 USD), description of what was purchased, and the
     payer's name + email. Plain text or simple HTML is fine — make it readable.
  c. Create a Gmail DRAFT (do NOT send) addressed to the customer's email. Voice:
     SynthLab Co — warm, professional, brief. Thank them for their payment,
     reference the amount and what it was for, and say the receipt is attached/
     on file. Sign off as SynthLab Co. Subject like
     "Your SynthLab Co receipt — <description>".
  d. Append this payment id to processed_ids.

STEP 5 — Persist memory.
- Write the updated state back to automation-runner/state/stripe-receipt.json
  with every newly handled id added to processed_ids. Preserve prior entries.

EDGE CASES.
- No payments in the window, or every payment already processed: do nothing and
  still succeed. Say briefly "nothing new" and end with FLOW_OK.
- A payment is missing a customer email: create the Drive receipt anyway, skip
  the email draft, note it, but still mark the id processed (the receipt is
  filed) so it is not retried forever.
- Never send email. Drafts only.

Finish with exactly FLOW_OK on success or FLOW_FAIL: <reason> if blocked.
