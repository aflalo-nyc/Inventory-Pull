# Pull Portal — Live Test Guide

> Updated 2026-09-14 for pulls with several items and the return form. The maintained,
> step-tagged version is the artifact: https://claude.ai/code/artifact/dd7f330f-2c45-4166-a5de-75e18b602b69

A run-through you can do with Lillian in about 15 minutes. It proves the whole loop:
form → alert email → stock check → approval → a real Shopify reservation → return →
the hold released. Nothing here can hurt real orders: the test moves **one unit** of
one item, and puts it back at the end.

---

## Before you start (5 minutes, do this alone)

**Step 0 — refresh the counts (agent).** Run `worker --once` (safe) right before you begin.
The **Available** number on the form and in the emails is a copy of Shopify's NYC count that
the agent refreshes each time it runs; it does not update itself. Until deploy it's only as
fresh as the last run. A stale count can never cause real damage: the actual Shopify
reservation uses Shopify's real number at that instant and refuses if it moved — worst case
is an approval bouncing back as Insufficient stock, never an oversell. After deploy the agent
runs every ~5 minutes and this step disappears.

**1. Pick the test item.** Open the **Pull Inventory** tab in the AFLALO Inventory
Pulls base and pick something with **Available of 3 or more** that nobody is about to
sell out of (an older style is perfect). Note its style, color, and size — in the form
you'll find it by typing any of those into the Item picker.

**2. Open three tabs side by side:**

| Tab | Where | What you'll watch |
|---|---|---|
| Pull Requests | Airtable base | Status changes, Worker Log |
| Pull Inventory | Airtable base | Available / Reserved counts |
| The product | Shopify admin → Products → your item → Inventory | The real NYC numbers moving |

**3. Write down the item's starting numbers** from the Shopify tab: Available and
Unavailable/Reserved at NYC. At the end of the test they must match these again.

**4. Have the worker ready.** The worker isn't on a schedule yet, so during the test
someone runs it by hand after each phase. Either message me and I run it when you say
go, or run it yourself in Terminal:

```
cd ~/Desktop/cs-email-agent
.venv/bin/python -m aflalo_pulls.worker --flow          # where every request stands + what's next
.venv/bin/python -m aflalo_pulls.worker --once          # SAFE preview (dry-run)
.venv/bin/python -m aflalo_pulls.worker --once --live   # actually moves inventory
```

`--flow` is the one to run whenever you're unsure: it lists each request, which step it's on
(1 requested, 2 approved, 3 out, 4 returned), and the exact next action.

Rule of thumb: everything uses the safe command until Test 4 says otherwise.

---

## Test 1 — Submit a request, alert fires

**Do:** Fill out the form: your email as requester, pick the item from the **Item** list
(search by style, color, or size), **Quantity 1** from the dropdown, Reason "Design
reference", Expected Return Date = tomorrow. Submit.

**Expect, within about a minute:**
- A new row in Pull Requests with Style / Color / Size / Available already filled in
  (they come through the Item link instantly). Status flips to **Requested**.
- **Lillian and Sarena each get an email** titled "New pull request: …" listing item,
  quantity, reason, return date, with a link that opens this exact record.
- When Lillian sets Approved, **Decided By** fills in with her name automatically.

**Then:** run the worker (safe command).

**Expect:** the row names itself (item / requester) and the terminal reports nothing to
move. Status stays **Requested** — it's waiting for a human decision.

☐ Pass

## Test 2 — The over-ask gets blocked instantly

**Do:** Submit the form again: pick an item whose **Available is under 5** and ask for **5**
(the dropdown's max). Then just watch — nothing to run.

**Expect, within seconds (all Airtable automations):**
- **Over-ask** shows 1 and Status flips to **Insufficient stock**. Sarena and Lillian get no
  email for this one — the alert only fires for requests that can be filled.
- **The requester (you) gets the "Not enough stock" email** with the real count.
- Nothing changed in Shopify. (If you run the worker, it double-checks and logs
  `requested 5, only N available` — a backstop, not the mechanism.)

☐ Pass

## Test 3 — Approval, previewed first

**Do:** Lillian opens the email from Test 1, clicks the record link, sets Status to
**Approved**. Run the worker with the **safe** command.

**Expect:** the terminal prints
`DRY-RUN reserve: would move for rec… — run with --live to execute`
and **nothing changes anywhere** — not in Airtable, not in Shopify. This is the worker
showing its homework before being allowed to act.

☐ Pass

## Test 4 — The real reservation

**Do:** Run the worker with the **live** command.

**Expect:**
- The row flips to **Reserved**, **Reserved At** is stamped, and Worker Log gains a
  line ending `reserve: ok`.
- **Shopify tab (refresh it):** the item's NYC **Available is down by 1** and
  **Unavailable/Reserved is up by 1** versus the numbers you wrote down.
- Pull Inventory tab shows the same after the run (the worker re-syncs every pass).

This is the moment the portal earns its keep: the unit physically can't be oversold
online while it's out on the pull.

☐ Pass

## Test 5 — (optional, 2 min) The overdue digest

**Do:** While the row is still Reserved, edit its **Expected Return Date to yesterday**.
Then: Automations tab → "Daily overdue pulls digest" → open it → press **Test** on the
trigger and let it run through.

**Expect:** Lillian + Sarena get "1 pull(s) past their expected return date" listing the
item, requester, quantity, and date. (In real life this fires by itself at 9am ET, and
only on days when something is actually overdue.)

Set the date back to tomorrow afterward.

☐ Pass

## Test 6 — Return: the hold comes off

**Do:** Set the row's Status to **Returned**. Run the worker with the **live** command.

**Expect:**
- Status flips to **Closed**, **Closed At** is stamped, and Worker Log gains `release: ok`.
  The worker never touches a closed row again.
- **Shopify tab (refresh):** Available and Unavailable/Reserved are back to **exactly
  the numbers you wrote down at the start**. The store is untouched.

☐ Pass

---

## If something doesn't match

| What you see | Why | Fix |
|---|---|---|
| No alert email after submitting | Automation is off, or the address is wrong | Automations tab → toggle on / fix the To address |
| Status never changes after a form submit | The worker hasn't run — nothing happens between worker runs | Run the worker, then re-check |
| Worker Log: `item not found in Pull Inventory — pick it from the list` | The row has no Item picked (legacy typed rows only) | Pick the item from the form's list |
| Worker Log: `reserve: FAILED …` mentioning quantities | Stock changed between the check and the move (something sold) — the safety guard refused rather than double-book | Run the worker again; it re-checks and retries with fresh numbers |
| Emails land in spam | First-time Airtable sender | Mark as not-spam once |

Nothing in the failure column ever moves inventory wrongly — the worker either does the
exact move it printed, or refuses and says why in Worker Log.

## When all six boxes are checked

Tell me, and I put the worker on an automatic schedule (every few minutes, `--live`).
From then on it's hands-off: form → email → approve → reserved, no terminal involved.
We also start chasing the legacy draft orders from June that the audit found.
