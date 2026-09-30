# Portal build decisions (answers received 2026-09-03)

| # | Decision | Build consequence |
| --- | --- | --- |
| 1 | Pulls come from Shopify NYC | CORRECTED at build: the store has THREE active locations (NYC, SAMPLE SALE, Wholesale). The worker pins to the one named NYC (override: PORTAL_LOCATION_NAME) and all availability numbers are NYC's |
| 2 | Anyone may request; expected users: jordyn@, anabella@ (brand), ava@, ana@ (merch), jasmine@ (founders office) | Open Airtable form; requester email is a required field |
| 3 | No extra fields needed | Form: item+size, quantity, reason, expected return date, requester |
| 4 | Reasons: stylist/celebrity loan, design reference, photoshoot | Single-select, extendable in Airtable without code |
| 5 | Approvers: Lillian + Sarena | Both get the request email; either can set Approved/Denied |
| 6 | One email per request, containing a link into Airtable to approve | Airtable automation on create; approving = setting Status on the linked record |
| 7 | Yes, reserve on approval even to zero, but ALWAYS show remaining units | Inventory table shows live Available/Reserved per size; the form links to it; approval email includes current availability |
| 8 | Block over-asks | See wrinkle A below |
| 9 | Overdue: daily email to requester + Lillian + Sarena | Airtable automation on an Overdue view, daily schedule |
| 10 | Never-returned items: notify Lillian, move to unavailable | Status "Write-off": worker moves reserved -> damaged (Shopify's unavailable bucket for stock that won't sell); automation emails Lillian |
| 11 | Run alongside draft orders; surface existing overdue drafts and email about them | Worker gets a draft-order audit (needs read_draft_orders on the portal app); output feeds the same reminder automation |
| 12 | Sanskriti + Sarena can create the Shopify app | Scopes below |

## The portal's own Shopify app (create in Dev Dashboard, install, same flow as before)

Name suggestion: `inventory-portal`. Scopes, all it needs and nothing more:

    write_inventory, read_inventory, read_products, read_locations, read_draft_orders

Client ID + secret go into `.env` as `PORTAL_SHOPIFY_API_KEY` / `PORTAL_SHOPIFY_API_SECRET`.
The CS agent's app is untouched and keeps zero write access.

## Two honest wrinkles in the answers

**A. "Block the form" (answer 8) cannot literally happen inside a native Airtable form** —
Airtable forms can't validate a number against another table at submit time. What we build
instead, which achieves the intent: the form shows live availability on the linked item, and
the worker validates every request the moment it lands. An over-ask is set to status
"Insufficient stock" within minutes and the requester is emailed automatically, before
Lillian ever sees it. Practical effect is a block; it just happens seconds after submit
rather than during it.

**B. "Move to unavailable" (answer 10) maps to Shopify's `damaged` state.** Shopify's
unavailable inventory is split into named buckets (damaged, quality_control, safety_stock,
reserved). For stock that left and isn't coming back, `damaged` is the bucket that keeps it
counted on-hand but never sellable, with the pull request's URI on the ledger entry. If
Ops prefers a true stock write-off (removing it from on-hand entirely), that's a one-line
change; default is the conservative one.

## Lifecycle (final)

    form submit ──► Requested ──► email Lillian + Sarena (link + current availability)
       │  worker validates quantity: over-ask ──► Insufficient stock ──► requester emailed
       ├─ Denied  ──► requester emailed the reason (automation)
       └─ Approved ──► worker reserves in Shopify (available -> reserved,
                        reason reservation_created, ledger = the request's own URL,
                        race-guarded by changeFromQuantity) ──► Reserved
              ├─ Returned  ──► worker releases (reserved -> available) ──► closed
              └─ Write-off ──► worker moves reserved -> damaged, Lillian notified

    daily: Overdue view (return date past, not Returned) ──► email requester + Lillian + Sarena
    daily: draft-order audit ──► open draft orders older than N days surfaced the same way

## Built 2026-09-03: base + automations

Portal base: **AFLALO Inventory Pulls** (appTrtbXNlwlgzfcG), separate from the CS
pipeline base. Four automations created as drafts (OFF until reviewed and enabled in the
Airtable UI — Automations tab):

1. New request → email Lillian + Sarena with the approve/deny link; blank Status
   normalized to Requested and the row named.
2. Status = Denied → requester gets the Denial Reason.
3. Status = Insufficient stock → requester gets the actual unit count (the worker sets
   this status; answer 8's "block the form", enforced minutes after submit).
4. Daily 9am ET → overdue digest (Reserved + past Expected Return Date) to
   Lillian + Sarena. Answer 9 asked for the requester on this too; the digest goes to
   the approvers, who see each requester's email per row. Per-requester overdue emails
   can be added later if wanted.

Approver addresses were assumed from the firstname@aflalonyc.com convention
(lillian@, sarena@) — VERIFY both before enabling.

## Changed 2026-09-11: the form got smarter

- **Item is a linked record** to Pull Inventory. In the form that is a searchable picker
  (type "gide", "plum", or "XS" and it narrows) — the answer to "separate item into style,
  color, size with autocomplete." A typed typo can no longer match nothing: the first real
  submission (2026-09-04, "Gide Sweater Plum") is exactly what this prevents.
- **Style / Color / Size / Available** on a request are lookups through that link, filled the
  instant the form lands — so the alert email to Lillian + Sarena can quote the live count
  ("Available" = as of the last worker sync) with no worker pass in between.
- **Pull Inventory** now carries Style, Color, Size from Shopify's own variant options.
- **Quantity is a 1–5 dropdown** (MAX_QUANTITY). Larger pulls are a conversation.
- **Decided By** records whoever last changed Status — approver or denier — automatically
  (an Airtable last-modified-by field; verified live: Sarena on both existing rows).
- **`worker --flow`** prints every request's step (1 requested → 2 approved → 3 reserved/out
  → 4 returned) and the exact next action, for testing and for "where is my pull?".
- The two early rows keep their typed text in `Item (legacy text)` / `Quantity (legacy)`;
  the worker still resolves them.

Still manual in the Airtable UI (the API can't): rebuild the form fields; in automation 1
swap the email's Item/Quantity tokens to the new fields and add Style/Color/Size/Available;
optionally turn off "allow linking to multiple records" on Item.

## Redesign 2026-09-14: pulls with several items, a return form, open/closed/overdue

Asked after the first supervised test. Everything below is built and live in the base.

**Tables**
- **Pull Orders** — one row per form submission: requester, reason, return date, and five
  Item/Qty slots (`Item 1..5` link to Pull Inventory, `Qty 1..5` 1–5). Formulas: `Items`
  (every item with quantity and live count, "← SHORT" where the ask exceeds stock),
  `Open lines`, `Overdue lines`, `State` (Pending → Open → Closed), `Return form link`
  (the return form pre-filled with this pull). The request form writes here.
- **Pull Requests** — one row per item ("line"): the unit approvers act on. New: `Order`
  link (items submitted together share a pull), `State` Open/Closed (the filter column),
  `Overdue` tag + `Days overdue`, `Return condition`. Status gained **Return submitted**
  (requester says it's back) and **Return accepted** (approver confirms; replaces
  "Returned" — delete the old "Returned" option in the UI).
- **Pull Returns** — the return form: `Pull` (pre-filled), `Returned?` Yes/No,
  `Return condition`. The worker processes each response once.
- **Pull Inventory** — gift cards excluded (Shopify `isGiftCard` / "gift card" type).

**Worker** (`--once`): syncs stock → fans each new order into lines against the fresh
count (short asks filed as Insufficient stock immediately, listed once on the order) →
turns "Yes" return responses into Return submitted lines → reserves Approved lines →
releases Return accepted / writes off Write-off → Closed.

**Emails (Airtable automations, all per PULL, never per item)**
1. Pull submitted → Sarena + Lillian (all items, live counts, approve link) + requester
   confirmation with the return-form link. Fires when the worker sets `Lines created`.
2. Short stock → requester, one email listing the short items.
3. Return day (9:00 ET) → requester, with the form link.
4. Overdue (9:05 ET) → requester (all items on the pull, cc Sarena + Lillian), daily
   until resolved.
5. Overdue digest (9:00 ET) → Sarena + Lillian, every overdue item with quantity,
   requester, days overdue.
6. Return form answered → Sarena + Lillian, with the Yes/No + notes.
7. Denied → requester (unchanged). Over-ask → Insufficient stock (per-line backstop).

**Honest wrinkles**
- The approver alert now waits for the worker's fan-out (≤5 min once deployed) — that is
  what makes the item rows exist and the counts fresh when they click through.
- Return-day and overdue emails are grouped per pull, not per requester: a requester with
  two overdue pulls gets two emails. Per-requester grouping would need a Requesters table.
- Airtable forms are UI-only: the request form must be rebuilt on Pull Orders and the
  return form created on Pull Returns, then `worker --return-form-url <share link>`.

**Partial approval is the normal case.** Items on one pull are decided one by one: two
Approved, one Denied, one Insufficient stock can all live on the same pull. Only approved
items are reserved; a "Yes" on the return form only flips items that were actually out
(Reserved); the pull reads Closed only when every item is closed by whatever path.
