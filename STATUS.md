# Inventory Pulls: status and next steps

Owner: Gloria Melidoni (handed over by Sanskriti Akhoury, 2026-10-02)
Last checked: 2026-10-05

## State: live, not used yet

| | |
|---|---|
| Request form | https://portal-production-18f8.up.railway.app (passcode + company email) |
| Service | Railway `aflalo-inventory-pulls` → `portal`, `/health` answers `ok` |
| Worker | **LIVE** since 2026-10-01: it moves real Shopify inventory. It syncs 679 NYC items every ~6 min. |
| Emails | All 9 Airtable automations deployed and valid (base `appTrtbXNlwlgzfcG`) |
| Tests | 27 pass |
| Real use | **None.** Pull Orders and Pull Requests are both empty. |

The supervised test in `docs/TESTING.md` was run on the 2026-09-14 version, which used Airtable's own
form. The current version (our own form, deployed 2026-09-30) has never had a pull go through it end
to end.

One Shopify `500` on 2026-10-01 04:32 UTC crashed a single pass; the next pass ran normally. Nothing
since.

## To finish

1. **One supervised pull with Lillian (~15 min).** Follow `docs/TESTING.md`, with two differences:
   the request goes through the portal link above, not an Airtable form, and Step 0 is not needed
   because the worker runs every 5 minutes. Pull **one unit** of an older style with 3+ available,
   then confirm, in order:
   - the approver email reaches Sarena + Lillian
   - Approved → the unit shows as reserved in Shopify within 5 min
   - the return form link in the requester's email opens that pull
   - Return accepted → the unit is back on sale
2. **Announce it.** Post the form link and passcode to the team in Slack.
3. **Decide when draft-order pulls stop.** The plan was to run both side by side at first
   (`docs/DECISIONS.md`, decision 11). `python -m aflalo_pulls.worker --draft-audit` lists the draft
   orders still open from the old process, so Lillian can close them out.

## Open, not blocking

- **Pushes don't deploy.** After any change, Railway → `portal` → Deploy by hand, or reconnect the
  GitHub source under Settings → Source with the account that owns the Railway project.
- **Repo is public.** Nothing secret in it, but make it private along with the other three, then
  confirm a Railway redeploy still builds.
- A requester with two overdue pulls gets two emails (grouping is per pull, not per person).
- Only ACTIVE Shopify products can be pulled. Draft or unlisted pieces are not in the picker.

## Where things are

Workflow, every ID, settings and common changes: `docs/PORTAL.html`. Why it works this way:
`docs/DECISIONS.md`.
