# Project 2 — Internal Inventory Pull Request Portal (scoping)

*Written 2026-09-01. Every API claim below was verified against the live AFLALO store's
own GraphQL schema or Shopify's documentation — sources cited inline. Nothing here is
assumed.*

## The problem, restated

Internal pulls (stylist/celebrity loans, design reference, photoshoot loans) are tracked
with draft orders. Draft orders have no approval step, no return date, and no owner, so
units drift: people forget to return them or forget to cancel the draft, and inventory
lies.

## The verdict on the unconfirmed part first

**Reserving inventory in Shopify on approval is feasible, and the mechanism is
purpose-built.** Evidence:

| Claim | How verified |
| --- | --- |
| Shopify inventory has a first-class `reserved` state, separate from `available` | Queried live on our store: `quantities(names: ["available","reserved","on_hand","committed"])` returns it (Tavi Pant: reserved 0 today) |
| A mutation exists to move units between states: `inventoryMoveQuantities` | Introspected live on our store's schema — present, with inputs `reason`, `referenceDocumentUri`, and per-change `inventoryItemId`, `quantity`, `from`/`to` (each: `locationId`, `name`, `ledgerDocumentUri`, `changeFromQuantity`) |
| Reservation is an *intended* use, not a hack | The documented reason values include `reservation_created`, `reservation_updated`, `reservation_deleted` (shopify.dev, manage-quantities-states) |
| Every reservation is individually auditable | `ledgerDocumentUri` is **required** when the state isn't `available` (docs: "used only … when the quantity name isn't 'available'") — each move must carry a URI naming its pull request. Returns reconcile per-request, which is exactly the discipline draft orders lack |
| Approvals are race-safe | `changeFromQuantity` — "the quantity currently expected at this location, before the move" (live schema description). Two simultaneous approvals of the last unit: the second fails instead of double-booking |
| A reserved unit can't be sold out from under the loan | Reserved subtracts from `available`, the number the storefront sells against — that is the definition of the state |
| Required permission: `write_inventory` | shopify.dev, inventoryMoveQuantities: "Requires `write_inventory` access scope" |

One honest caveat from the docs: Shopify's guidance suggests draft orders for *POS
customer* holds rather than manual `reserved` moves. Our case is the opposite shape —
internal, approval-gated, dated, and needing an audit trail per request — which is what
the `reservation_*` reasons and mandatory ledger URIs exist for. Worth stating to anyone
who asks why we're not "just using draft orders": draft orders are the current system,
and their failure is the reason this project exists.

## The lifecycle, mapped to verified API calls

```
REQUESTED ──(Lillian denies)──► DENIED       requester emailed the reason, nothing touched
    │
    └──(Lillian approves)─────► RESERVED     inventoryMoveQuantities:
                                              available ── N units ──► reserved
                                              reason: reservation_created
                                              ledgerDocumentUri: the request's own URL
                                              changeFromQuantity: guard against races
    │
    └──(units come back)──────► RETURNED     the reverse move, reason: reservation_deleted
```

Overdue = `expected return date` past and not RETURNED. That's a filtered view and a
nagging email, and it is the entire fix for "people forget to return units": the system
knows who has what, why, and since when, because every reservation names its request.

## Recommended architecture: Airtable-native portal + one small worker

The team already lives in the Airtable base. Build the portal where they are:

| Piece | What | Built with |
| --- | --- | --- |
| `Inventory` table | Live availability per variant (available / reserved / incoming), synced from Shopify on a schedule | the worker, read-only calls we already have working today |
| Request form | Native Airtable form: requester, piece + size (linked to the Inventory table so **availability is visible before submitting**), units, reason for request, expected return date | Airtable, no code |
| Alert to Lillian | Automation on new request → email | Airtable automations send email natively — **no Gmail send permission anywhere in this system** |
| Deny path | Lillian sets Status = Denied + a reason field → automation emails the requester the reason | Airtable, no code |
| Approve path | Lillian sets Status = Approved → the worker makes the verified `inventoryMoveQuantities` call, stamps the row Reserved | ~150 lines in this repo (`portal/`), reusing the existing Shopify client |
| Return path | Status = Returned → reverse move, row closed | same worker |
| Overdue view | Return date past, not Returned → visible list + reminder email | Airtable, no code |

The only code is the worker: poll the table, execute approved/returned moves, sync the
Inventory table. It can run on the same always-on loop planned for the CS agent.

## Credentials — deliberately separate

The CS agent's app is read-only by construction, and that guarantee is worth keeping
absolute. The portal needs `write_inventory`, so it gets **its own Shopify app**
(`inventory-portal`): `write_inventory`, `read_inventory`, `read_products`,
`read_locations` (location IDs are required by the move call, and our current app cannot
read locations — verified: the field is scope-gated). Same 2-minute Dev Dashboard flow we
have done twice. A bug in the portal could then at worst mis-move inventory counts; it
still could never touch orders, refunds, customers, or email.

## What still needs confirming (small, listed honestly)

1. **Single location?** The move call is per-location. If all sellable stock lives in one
   location this is trivial; if not, the form needs a location choice. One query answers
   this once `read_locations` exists.
2. **Live dry-run of the move itself.** The mutation, its inputs, and its reasons are
   verified; the final proof is one reversible move of 1 unit on a test variant
   (available → reserved → available), done during build, watched in the admin UI.
3. **Lillian's flow preferences** — approve from the email? a daily digest? Both are
   Airtable-automation options, zero code either way.

## Effort estimate

Tables + form + automations: half a day. Worker with dry-run mode + tests: a day.
Supervised live test on one SKU, then open to the team: half a day. The pattern
(Airtable as the surface, a small grounded worker doing verified API calls, humans making
the decisions) is the same one already running for CS.
