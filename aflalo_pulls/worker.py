"""The portal worker: the only code between the Airtable surface and Shopify.

    python -m aflalo_pulls.worker --setup        create/upgrade all four tables, print next steps
    python -m aflalo_pulls.worker --flow         where every pull stands + what happens next
    python -m aflalo_pulls.worker --once         one pass: fan out, validate, reserve, release (DRY-RUN)
    python -m aflalo_pulls.worker --once --live  the same, actually moving inventory
    python -m aflalo_pulls.worker --return-form-url URL   point the emails at the return form
    python -m aflalo_pulls.worker --draft-audit  list open draft orders (the legacy system)

Dry-run is the DEFAULT: it prints every move it would make and touches nothing.

THE SHAPE (redesigned 2026-09-14 after the first supervised test):

  Pull Orders    one row per FORM SUBMISSION — requester, reason, return date, and up to
                 five Item/Qty slots. The request form writes here.
  Pull Requests  one row per ITEM (a "line") — the unit that is approved, reserved,
                 returned, and closed. The worker fans an order out into its lines.
  Pull Returns   one row per RETURN FORM response — "Have you returned the items?
                 Yes/No + condition notes", linked to the pull.
  Pull Inventory live NYC counts per variant, synced from Shopify (gift cards excluded).

Everything human stays in Airtable automations: the approver alert (one email per pull,
all items), the requester confirmation with the return-form link, the short-stock notice,
the return-day reminder, the overdue emails (grouped per pull), the denial email. This
worker only (1) fans orders out into lines against fresh stock, (2) turns "Yes, returned"
form responses into Return submitted lines, (3) executes approved / accepted / write-off
moves in Shopify, (4) keeps Pull Inventory fresh.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .airtable import API, Airtable
from .shopify_inventory import PortalShopify, portal_client

ORDERS_TABLE = "Pull Orders"
REQUESTS_TABLE = "Pull Requests"      # the line items
RETURNS_TABLE = "Pull Returns"
INVENTORY_TABLE = "Pull Inventory"
MAX_QUANTITY = 5      # the form's dropdown stops here (team, 2026-09-11)
SLOTS = 5             # items per form submission (team, 2026-09-14)
QTY_OPTIONS = [str(n) for n in range(1, MAX_QUANTITY + 1)]
REASONS = ["Stylist/celebrity loan", "Design reference", "Photoshoot"]
# The portal's own web pages (aflalo_pulls/web.py). PORTAL_URL is where they are hosted;
# until it is set, the formula carries a placeholder that is obviously not a link.
PORTAL_URL = os.environ.get("PORTAL_URL", "https://SET-PORTAL_URL").rstrip("/")
GIFT_CARD_RE = re.compile(r"gift\s*card", re.I)

# Line statuses. Humans set Approved / Denied / Return accepted / Write-off; the worker
# sets everything else. "Returned" was renamed "Return accepted" (team, 2026-09-14): the
# requester SAYS returned via the form (-> Return submitted), an approver CONFIRMS it.
LINE_STATUSES = ["Requested", "Approved", "Denied", "Insufficient stock", "Reserved",
                 "Return submitted", "Return accepted", "Write-off", "Closed"]
OPEN_STATUSES = {"Requested", "Approved", "Reserved", "Return submitted", "Return accepted", "Write-off"}

# ---- schemas ----------------------------------------------------------------------------
# Plain fields go through airtable.py's schema format. Fields that need another
# table's id (links, lookups) or a formula are declared in DERIVED below and created by
# setup() once every table exists.

REQUEST_SCHEMA: list[dict[str, Any]] = [
    {"name": "Request", "type": "singleLineText", "note": "item / requester. Named by the worker."},
    {"name": "Requester Email", "type": "email", "note": "Copied from the pull order."},
    {"name": "Quantity", "type": "singleSelect", "note": f"1 to {MAX_QUANTITY}.", "options": QTY_OPTIONS},
    {"name": "Reason", "type": "singleSelect", "note": "Copied from the pull order.", "options": REASONS},
    {"name": "Expected Return Date", "type": "date", "note": "Copied from the pull order. Drives the return-day and overdue emails."},
    {"name": "Status", "type": "singleSelect", "note": "Approvers set Approved / Denied / Return accepted / Write-off. The worker sets the rest.",
     "options": LINE_STATUSES},
    {"name": "Denial Reason", "type": "multilineText", "note": "Filled by the approver on Deny; emailed to the requester."},
    {"name": "Return condition", "type": "multilineText", "note": "What the requester wrote on the return form."},
    {"name": "Worker Log", "type": "multilineText", "note": "What the worker did and when, including the Shopify move result."},
    {"name": "Reserved At", "type": "dateTime", "note": ""},
    {"name": "Closed At", "type": "dateTime", "note": "Units released back to sale, or written off."},
]

ORDER_FIELDS: list[dict[str, Any]] = [
    {"name": "Pull #", "type": "autoNumber", "description": "The pull's number. One row per form submission."},
    {"name": "Requester Email", "type": "email", "description": "Gets the confirmation, short-stock, return-day, and overdue emails."},
    {"name": "Reason", "type": "singleSelect", "options": {"choices": [{"name": r} for r in REASONS]}},
    {"name": "Expected Return Date", "type": "date", "options": {"dateFormat": {"name": "iso"}}},
    *[{"name": f"Qty {n}", "type": "singleSelect", "options": {"choices": [{"name": q} for q in QTY_OPTIONS]},
       "description": f"Quantity for Item {n}."} for n in range(1, SLOTS + 1)],
    {"name": "Lines created", "type": "checkbox", "options": {"icon": "check", "color": "greenBright"},
     "description": "Ticked by the worker once every item has its own row in Pull Requests. The approver alert fires on this."},
    {"name": "Short lines", "type": "multilineText", "description": "Items that asked for more than NYC has; written by the worker, emailed to the requester."},
    {"name": "Return response", "type": "multilineText", "description": "The latest return-form answer: Yes/No and the condition notes."},
    {"name": "Return response at", "type": "dateTime", "options": {"timeZone": "client", "dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}},
     "description": "When the latest return form came in. Approvers are emailed on each change."},
    {"name": "Submitted", "type": "createdTime"},
]

RETURN_FIELDS: list[dict[str, Any]] = [
    {"name": "Return #", "type": "autoNumber"},
    {"name": "Returned?", "type": "singleSelect", "options": {"choices": [{"name": "Yes"}, {"name": "No"}]},
     "description": "Yes -> the pull's reserved items become Return submitted for an approver to accept."},
    {"name": "Return condition", "type": "multilineText", "description": "Condition of the pieces, or anything else to tell Sarena and Lillian."},
    {"name": "Processed", "type": "checkbox", "options": {"icon": "check", "color": "greenBright"},
     "description": "Ticked by the worker once it has acted on this response."},
    {"name": "Submitted at", "type": "createdTime"},
]

INVENTORY_SCHEMA: list[dict[str, Any]] = [
    {"name": "Item", "type": "singleLineText", "note": "Style / Color / Size — the full name the form searches on. The key."},
    {"name": "Style", "type": "singleLineText", "note": "The product, e.g. 'Gide Sweater in Wool'."},
    {"name": "Color", "type": "singleLineText", "note": "Shopify's Color option."},
    {"name": "Size", "type": "singleLineText", "note": "Shopify's Size option."},
    {"name": "Available", "type": "number", "precision": 0, "note": "Sellable right now at NYC. What requesters check before asking."},
    {"name": "Reserved", "type": "number", "precision": 0, "note": "Held by approved pulls."},
    {"name": "Incoming", "type": "number", "precision": 0, "note": "Replenishment on the way."},
    {"name": "Inventory Item ID", "type": "singleLineText", "note": "Shopify id the worker moves against."},
    {"name": "Updated At", "type": "dateTime", "note": "Freshness of this row."},
    {"name": "Active", "type": "checkbox", "note": "Seen in Shopify on the last sync. Unticked = deactivated; Available forced to 0."},
]


def _items_formula() -> str:
    # One line per filled slot: "• Mira Jacket in Wool Silk / Olive / S  x2  (available 3)",
    # with "← SHORT" when the ask exceeds the count. Blank slots render nothing.
    parts = []
    for n in range(1, SLOTS + 1):
        parts.append(
            f'IF({{Item {n}}}, "• " & {{Item {n}}} & "  x" & {{Qty {n}}} & "  (available " & {{Available {n}}} & ")"'
            f' & IF(VALUE({{Qty {n}}} & "") > VALUE(ARRAYJOIN({{Available {n}}}) & ""), "  ← SHORT", "") & "\\n", "")'
        )
    return " & ".join(parts)


def _has_short_formula() -> str:
    conds = [f'AND({{Item {n}}}, VALUE({{Qty {n}}} & "") > VALUE(ARRAYJOIN({{Available {n}}}) & ""))'
             for n in range(1, SLOTS + 1)]
    return f"IF(OR({', '.join(conds)}), 1, 0)"


# Created after all tables exist, in this order (later entries reference earlier ones).
# kinds: link (to another table), lookup (through a link on this table), formula.
DERIVED: list[dict[str, Any]] = [
    # lines
    {"table": REQUESTS_TABLE, "name": "Item", "kind": "link", "to": INVENTORY_TABLE,
     "description": "The exact piece, from Pull Inventory."},
    {"table": REQUESTS_TABLE, "name": "Order", "kind": "link", "to": ORDERS_TABLE,
     "description": "The pull this item belongs to. Items submitted together share one pull."},
    {"table": REQUESTS_TABLE, "name": "Style", "kind": "lookup", "via": "Item", "source": "Style", "description": "From the picked item."},
    {"table": REQUESTS_TABLE, "name": "Color", "kind": "lookup", "via": "Item", "source": "Color", "description": "From the picked item."},
    {"table": REQUESTS_TABLE, "name": "Size", "kind": "lookup", "via": "Item", "source": "Size", "description": "From the picked item."},
    {"table": REQUESTS_TABLE, "name": "Available", "kind": "lookup", "via": "Item", "source": "Available",
     "description": "Live count from Pull Inventory (as of the last worker sync)."},
    {"table": REQUESTS_TABLE, "name": "Decided By", "kind": "last_modified_by", "watch": "Status",
     "description": "Whoever last changed Status — the approver or denier. Automatic."},
    {"table": REQUESTS_TABLE, "name": "Over-ask", "kind": "formula",
     "formula": 'IF(AND({Quantity}, LEN(ARRAYJOIN({Available})) > 0), IF(VALUE({Quantity}) > VALUE(ARRAYJOIN({Available})), 1, 0), 0)',
     "description": "1 when the ask exceeds Available (including Available 0). Backstop; the worker already files these as Insufficient stock."},
    {"table": REQUESTS_TABLE, "name": "Is open", "kind": "formula",
     "formula": 'IF(OR({Status} = "Closed", {Status} = "Denied", {Status} = "Insufficient stock"), 0, 1)',
     "description": "1 while the item still needs something from someone."},
    {"table": REQUESTS_TABLE, "name": "State", "kind": "formula",
     "formula": 'IF({Is open} = 1, "Open", "Closed")',
     "description": "Open or Closed — filter on this. Status says which step an open item is at."},
    {"table": REQUESTS_TABLE, "name": "Is overdue", "kind": "formula",
     "formula": 'IF(AND({Status} = "Reserved", IS_BEFORE({Expected Return Date}, TODAY())), 1, 0)',
     "description": "1 when the piece is out past its return date and no return form has been submitted."},
    {"table": REQUESTS_TABLE, "name": "Overdue", "kind": "formula",
     "formula": 'IF({Is overdue} = 1, "OVERDUE", "")',
     "description": "The tag. Reads OVERDUE while Is overdue is 1."},
    {"table": REQUESTS_TABLE, "name": "Days overdue", "kind": "formula",
     "formula": 'IF({Is overdue} = 1, DATETIME_DIFF(TODAY(), {Expected Return Date}, "days"), BLANK())',
     "description": ""},
    # orders
    *[{"table": ORDERS_TABLE, "name": f"Item {n}", "kind": "link", "to": INVENTORY_TABLE,
       "description": f"Item {n} on the form. Search by style, color, or size."} for n in range(1, SLOTS + 1)],
    *[{"table": ORDERS_TABLE, "name": f"Available {n}", "kind": "lookup", "via": f"Item {n}", "source": "Available",
       "description": f"Live count for Item {n}."} for n in range(1, SLOTS + 1)],
    {"table": ORDERS_TABLE, "name": "Items", "kind": "formula", "formula": _items_formula(),
     "description": "Every item on this pull with its quantity and live count. This is what the emails show."},
    {"table": ORDERS_TABLE, "name": "Has short", "kind": "formula", "formula": _has_short_formula(),
     "description": "1 when any item asks for more than NYC has."},
    {"table": ORDERS_TABLE, "name": "Open flags", "kind": "lookup", "via": "Lines", "source": "Is open", "description": ""},
    {"table": ORDERS_TABLE, "name": "Overdue flags", "kind": "lookup", "via": "Lines", "source": "Is overdue", "description": ""},
    {"table": ORDERS_TABLE, "name": "Open lines", "kind": "formula", "formula": "SUM({Open flags})",
     "description": "How many items on this pull are still open."},
    {"table": ORDERS_TABLE, "name": "Overdue lines", "kind": "formula", "formula": "SUM({Overdue flags})",
     "description": "How many items on this pull are out past their return date."},
    {"table": ORDERS_TABLE, "name": "State", "kind": "formula",
     "formula": 'IF({Lines created}, IF({Open lines} > 0, "Open", "Closed"), "Pending")',
     "description": "Open while any item is open; Pending until the worker has created the item rows."},
    {"table": ORDERS_TABLE, "name": "Return form link", "kind": "formula",
     "formula": f'"{PORTAL_URL}/return/" & RECORD_ID()',
     "description": "The portal's return page for this pull. Re-point with `worker --return-form-url <portal url>`."},
    # returns
    {"table": RETURNS_TABLE, "name": "Pull", "kind": "link", "to": ORDERS_TABLE,
     "description": "Which pull is being returned. Pre-filled from the link in your emails."},
]

INVENTORY_SYNC_QUERY = """
query Inv($cursor: String, $loc: ID!) {
  products(first: 40, after: $cursor, query: "status:active") {
    pageInfo { hasNextPage endCursor }
    edges { node { title productType isGiftCard
      variants(first: 50) { edges { node { title
        selectedOptions { name value }
        inventoryItem { id inventoryLevel(locationId: $loc) {
          quantities(names: ["available", "reserved", "incoming"]) { name quantity }
        } }
      } } }
    } }
  }
}
"""

# Where a line is in the flow, from its Status, and what moves it forward.
STAGES: dict[str, tuple[int, str, str]] = {
    "Requested":          (1, "waiting for Sarena or Lillian to set Approved / Denied",
                              "an approver opens the pull (link in their email)"),
    "Approved":           (2, "approved — units not yet reserved in Shopify",
                              "the worker: `--once --live` reserves them"),
    "Insufficient stock": (0, "closed — asked for more than NYC has; requester was emailed",
                              "nothing (requester can resubmit for fewer)"),
    "Denied":             (0, "closed — requester was emailed the reason", "nothing"),
    "Reserved":           (3, "units held in Shopify; piece is out",
                              "the requester submits the return form when it comes back"),
    "Return submitted":   (4, "requester says it's back — waiting for an approver to confirm",
                              "Sarena or Lillian set Status = Return accepted (or Write-off)"),
    "Return accepted":    (5, "return confirmed — hold not yet released",
                              "the worker: `--once --live` puts the units back on sale"),
    "Write-off":          (5, "marked write-off — units not yet moved to damaged",
                              "the worker: `--once --live` moves them to Shopify's damaged bucket"),
    "Closed":             (0, "done — units back on sale (or written off); Closed At says when", "nothing"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _tables(token: str, base: str) -> dict[str, Airtable]:
    return {
        ORDERS_TABLE: Airtable(token, base, ORDERS_TABLE, [], key_field="Pull #",
                               description="One row per pull request form submission (up to five items). "
                               "The worker fans each into item rows in Pull Requests."),
        REQUESTS_TABLE: Airtable(token, base, REQUESTS_TABLE, REQUEST_SCHEMA, key_field="Request",
                                 description="One row per requested item. Approvers work here: Approved / Denied / "
                                 "Return accepted / Write-off. The worker reserves and releases in Shopify."),
        RETURNS_TABLE: Airtable(token, base, RETURNS_TABLE, [], key_field="Return #",
                                description="Return form responses. 'Yes' turns the pull's reserved items into "
                                "Return submitted for an approver to confirm."),
        INVENTORY_TABLE: Airtable(token, base, INVENTORY_TABLE, INVENTORY_SCHEMA, key_field="Item",
                                  description="Live availability per item/size, synced by the pull worker. "
                                  "Requesters: check here before submitting."),
    }


# ---- schema setup -------------------------------------------------------------------------

def _meta(token: str, base: str, method: str, path: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}/meta/bases/{base}/{path}", method=method,
        data=json.dumps(payload).encode() if payload else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Airtable {exc.code} on {method} {path}: {exc.read()[:300].decode()}") from exc


def _schema(token: str, base: str) -> dict[str, dict]:
    return {t["name"]: t for t in _meta(token, base, "GET", "tables")["tables"]}


def _ensure_table(token: str, base: str, name: str, fields: list[dict], description: str) -> str:
    tables = _schema(token, base)
    # createdTime can't ride along on table creation; it (and anything else missing) is
    # added field by field afterwards, which also makes this an upgrade path.
    if name not in tables:
        first = [f for f in fields if f["type"] != "createdTime"]
        tid = _meta(token, base, "POST", "tables", {"name": name, "description": description, "fields": first})["id"]
        tables = _schema(token, base)
    tid = tables[name]["id"]
    have = {f["name"] for f in tables[name]["fields"]}
    for f in fields:
        if f["name"] not in have and f["type"] != "autoNumber":
            _meta(token, base, "POST", f"tables/{tid}/fields", f)
    return tid


def setup(token: str, base: str, tables: dict[str, Airtable]) -> list[str]:
    """Create or upgrade all four tables. Idempotent; returns what it created."""
    made: list[str] = []
    tables[REQUESTS_TABLE].ensure_fields(tables[REQUESTS_TABLE].create_table())
    tables[INVENTORY_TABLE].ensure_fields(tables[INVENTORY_TABLE].create_table())
    _ensure_table(token, base, ORDERS_TABLE, ORDER_FIELDS, tables[ORDERS_TABLE].description)
    _ensure_table(token, base, RETURNS_TABLE, RETURN_FIELDS, tables[RETURNS_TABLE].description)

    expected_type = {"link": "multipleRecordLinks", "lookup": "multipleLookupValues",
                     "formula": "formula", "last_modified_by": "lastModifiedBy"}
    for spec in DERIVED:
        schema = _schema(token, base)
        t = schema[spec["table"]]
        names = {f["name"]: f for f in t["fields"]}
        if spec["name"] in names:
            if names[spec["name"]]["type"] == expected_type[spec["kind"]]:
                continue
            # Deleting a linked table turns every link to it into plain text (happened
            # 2026-09-17: Pull Orders was deleted by hand). The API can't delete fields,
            # so the husk is renamed out of the way and the real field recreated.
            husk = names[spec["name"]]
            _meta(token, base, "PATCH", f"tables/{t['id']}/fields/{husk['id']}",
                  {"name": f"{spec['name']} (broken link, safe to delete)",
                   "description": "Left behind when its linked table was deleted. Delete this column."})
            made.append(f"{spec['table']}.{spec['name']} (renamed husk)")
        body: dict[str, Any] = {"name": spec["name"], "description": spec.get("description", "")}
        if spec["kind"] == "link":
            body.update(type="multipleRecordLinks", options={"linkedTableId": schema[spec["to"]]["id"]})
        elif spec["kind"] == "lookup":
            via = names[spec["via"]]
            target = schema[next(x["name"] for x in schema.values() if x["id"] == via["options"]["linkedTableId"])]
            body.update(type="multipleLookupValues", options={
                "recordLinkFieldId": via["id"],
                "fieldIdInLinkedTable": next(f["id"] for f in target["fields"] if f["name"] == spec["source"])})
        elif spec["kind"] == "formula":
            body.update(type="formula", options={"formula": spec["formula"]})
        else:
            body.update(type="lastModifiedBy", options={"referencedFieldIds": [names[spec["watch"]]["id"]]})
        _meta(token, base, "POST", f"tables/{t['id']}/fields", body)
        made.append(f"{spec['table']}.{spec['name']}")
        if spec["kind"] == "link" and spec["name"] == "Order":
            # Airtable auto-creates the reverse link on Pull Orders, named after this table.
            # Every order-side lookup goes through it, so give it its real name now.
            schema = _schema(token, base)
            for f in schema[ORDERS_TABLE]["fields"]:
                if f["type"] == "multipleRecordLinks" and f["name"] != "Lines" \
                        and f["options"].get("linkedTableId") == schema[REQUESTS_TABLE]["id"]:
                    _meta(token, base, "PATCH", f"tables/{schema[ORDERS_TABLE]['id']}/fields/{f['id']}",
                          {"name": "Lines", "description": "The item rows of this pull."})
                    made.append(f"{ORDERS_TABLE}.Lines")

    made += _sync_status_options(token, base)
    return made


def _sync_status_options(token: str, base: str) -> list[str]:
    """Rename 'Returned' -> 'Return accepted' (same choice id, so existing rows follow) and
    add any status the worker knows that the table doesn't."""
    schema = _schema(token, base)
    t = schema[REQUESTS_TABLE]
    status = next(f for f in t["fields"] if f["name"] == "Status")
    choices = status["options"]["choices"]
    changed = []
    for c in choices:
        if c["name"] == "Returned":
            c["name"] = "Return accepted"; changed.append("Status: Returned -> Return accepted")
    have = {c["name"] for c in choices}
    for s in LINE_STATUSES:
        if s not in have:
            choices.append({"name": s}); changed.append(f"Status: + {s}")
    if changed:
        # The REST Meta API refuses option edits on an existing select ("changing a field's
        # type is not supported"). New options appear on their own the first time the worker
        # writes them (typecast); a RENAME has to be done in the Airtable UI or via the
        # connector — so this only reports, it doesn't fail the setup.
        try:
            _meta(token, base, "PATCH", f"tables/{t['id']}/fields/{status['id']}",
                  {"options": {"choices": [{k: v for k, v in c.items() if k in ("id", "name", "color")} for c in choices]}})
        except RuntimeError:
            return [f"NEEDS UI: {c}" for c in changed]
    return changed


def set_return_form_url(token: str, base: str, url: str) -> None:
    schema = _schema(token, base)
    t = schema[ORDERS_TABLE]
    f = next(x for x in t["fields"] if x["name"] == "Return form link")
    _meta(token, base, "PATCH", f"tables/{t['id']}/fields/{f['id']}",
          {"options": {"formula": f'"{url.rstrip("/")}/return/" & RECORD_ID()'}})


# ---- inventory sync -----------------------------------------------------------------------

def is_gift_card(product: dict) -> bool:
    return bool(product.get("isGiftCard")) or bool(GIFT_CARD_RE.search(
        f"{product.get('productType') or ''} {product.get('title') or ''}"))


def sync_inventory(shop: PortalShopify, inventory: Airtable) -> int:
    location = shop.single_location_id()   # quantities at the PULL location, not anywhere
    rows, cursor = [], None
    while True:
        body = shop._graphql_vars(INVENTORY_SYNC_QUERY, {"cursor": cursor, "loc": location})
        if body.get("errors"):
            raise RuntimeError(str(body["errors"][0].get("message", ""))[:150])
        page = body["data"]["products"]
        for e in page["edges"]:
            product = e["node"]
            if is_gift_card(product):      # not a garment; nobody pulls a gift card (team, 2026-09-14)
                continue
            for v in product["variants"]["edges"]:
                vn = v["node"]
                level = vn["inventoryItem"].get("inventoryLevel") or {}
                q = {x["name"]: x["quantity"] for x in level.get("quantities", [])}
                opts = {o["name"].lower(): o["value"] for o in vn.get("selectedOptions", [])}
                rows.append({
                    "Item": f"{product['title']} / {vn['title']}"[:250],
                    "Style": product["title"][:250],
                    "Color": opts.get("color", ""),
                    "Size": opts.get("size", ""),
                    "Available": q.get("available", 0),
                    "Reserved": q.get("reserved", 0),
                    "Incoming": q.get("incoming", 0),
                    "Inventory Item ID": vn["inventoryItem"]["id"],
                    "Updated At": _now(),
                    "Active": True,
                })
        if not page["pageInfo"]["hasNextPage"]:
            break
        cursor = page["pageInfo"]["endCursor"]
    # Variants Shopify no longer lists (deactivated, deleted, or now excluded) must not stay
    # pickable with a stale count: kept (old lines link to them) but retired to 0.
    seen = {r["Item"] for r in rows}
    for item in inventory.existing_ids():
        if item not in seen:
            rows.append({"Item": item, "Available": 0, "Reserved": 0, "Incoming": 0,
                         "Updated At": _now(), "Active": False})
    inventory.push(rows, row_fn=lambda r: r)
    return len(seen)


# ---- record helpers -----------------------------------------------------------------------

def _record_url(base: str, table_id: str, record_id: str) -> str:
    return f"https://airtable.com/{base}/{table_id}/{record_id}"


def _fetch(table: Airtable, formula: str) -> list[dict]:
    out, offset = [], None
    while True:
        params: dict = {"pageSize": 100, "filterByFormula": formula}
        if offset:
            params["offset"] = offset
        data = table._call("GET", table.table, params=params)
        out += data.get("records", [])
        offset = data.get("offset")
        if not offset:
            return out


def _by_status(lines: Airtable, status: str) -> list[dict]:
    return _fetch(lines, f"{{Status}} = '{status}'")


def _get(table: Airtable, record_id: str) -> dict:
    return table._call("GET", f"{table.table}/{record_id}").get("fields", {})


def _patch(table: Airtable, record_id: str, fields: dict) -> None:
    table._call("PATCH", f"{table.table}/{record_id}", {"fields": fields, "typecast": True})


def _create(table: Airtable, fields: dict) -> str:
    return table._call("POST", table.table, {"records": [{"fields": fields}], "typecast": True})["records"][0]["id"]


def _stock_for(inventory: Airtable, f: dict) -> dict | None:
    """The Pull Inventory row this line points at: through the Item link, else by the
    exact name typed on the two pre-picker rows."""
    item = f.get("Item")
    if isinstance(item, list) and item:
        return _get(inventory, item[0])
    name = item if isinstance(item, str) else f.get("Item (legacy text)")
    if not name:
        return None
    recs = _fetch(inventory, f"{{Item}} = '{name}'")
    return recs[0]["fields"] if recs else None


def _qty(f: dict) -> int:
    raw = f.get("Quantity")
    if raw in (None, ""):
        raw = f.get("Quantity (legacy)") or 0
    return int(str(raw).strip() or 0)


def _item_name(f: dict, stock: dict | None) -> str:
    if stock and stock.get("Item"):
        return stock["Item"]
    item = f.get("Item")
    return item if isinstance(item, str) else (f.get("Item (legacy text)") or "?")


# ---- the pass -------------------------------------------------------------------------------

def fan_out(orders: Airtable, lines: Airtable, inventory: Airtable) -> list[str]:
    """Every new form submission becomes one line per filled Item/Qty slot, each checked
    against fresh stock. Short asks are filed as Insufficient stock straight away and
    listed on the order so the requester gets ONE email, not one per item."""
    log: list[str] = []
    for order in _fetch(orders, "NOT({Lines created})"):
        f, oid = order["fields"], order["id"]
        try:
            short, created = [], 0
            for n in range(1, SLOTS + 1):
                item, qty = f.get(f"Item {n}"), f.get(f"Qty {n}")
                if not (isinstance(item, list) and item and qty):
                    continue
                stock = _get(inventory, item[0])
                available, q = int(stock.get("Available") or 0), int(str(qty))
                ok = q <= available
                if not ok:
                    short.append(f"{stock.get('Item', '?')}: asked {q}, only {available} available")
                _create(lines, {
                    "Request": f"{stock.get('Item', '?')} / {f.get('Requester Email', '?')}"[:250],
                    "Order": [oid], "Item": [item[0]], "Quantity": str(q),
                    "Requester Email": f.get("Requester Email"), "Reason": f.get("Reason"),
                    "Expected Return Date": f.get("Expected Return Date"),
                    "Status": "Requested" if ok else "Insufficient stock",
                    "Worker Log": f"{_now()} created from pull #{f.get('Pull #')}"
                                  + ("" if ok else f" — requested {q}, only {available} available") + "\n",
                })
                created += 1
            fields: dict[str, Any] = {"Short lines": "\n".join(short)}
            if created:
                fields["Lines created"] = True
            else:
                fields["Short lines"] = "No items were picked on the form, so nothing was requested."
            _patch(orders, oid, fields)
            log.append(f"pull #{f.get('Pull #')}: {created} item(s) created, {len(short)} short")
        except Exception as exc:  # one bad order must not stall the rest
            log.append(f"pull {oid} fan-out failed: {exc}")
    return log


def process_returns(returns: Airtable, orders: Airtable, lines: Airtable) -> list[str]:
    """'Yes, returned' on the form -> the pull's reserved items become Return submitted for an
    approver to confirm. Either answer is copied to the order so approvers get one email."""
    log: list[str] = []
    for rec in _fetch(returns, "NOT({Processed})"):
        f, rid = rec["fields"], rec["id"]
        try:
            pull = f.get("Pull")
            answer, notes = (f.get("Returned?") or "?"), (f.get("Return condition") or "").strip()
            if not (isinstance(pull, list) and pull):
                _patch(returns, rid, {"Processed": True})
                log.append(f"return {rid}: no pull linked — ignored")
                continue
            order = _get(orders, pull[0])
            moved = 0
            if answer == "Yes":
                for line_id in order.get("Lines") or []:
                    lf = _get(lines, line_id)
                    if lf.get("Status") == "Reserved":
                        _patch(lines, line_id, {
                            "Status": "Return submitted", "Return condition": notes,
                            "Worker Log": (lf.get("Worker Log") or "") + f"{_now()} return form: returned — awaiting an approver\n"})
                        moved += 1
            _patch(orders, pull[0], {
                "Return response": f"{answer}" + (f" — {notes}" if notes else ""),
                "Return response at": _now()})
            _patch(returns, rid, {"Processed": True})
            log.append(f"pull #{order.get('Pull #')}: return form says {answer}; {moved} item(s) -> Return submitted")
        except Exception as exc:
            log.append(f"return {rid} failed: {exc}")
    return log


def process(shop: PortalShopify, lines: Airtable, inventory: Airtable,
            base: str, table_id: str, live: bool) -> list[str]:
    log: list[str] = []
    location = shop.single_location_id()
    blocked: set[str] = set()   # flipped to Insufficient stock THIS pass; never reserve them

    def act(label: str, rec: dict, fn, ok_fields: dict) -> None:
        rid = rec["id"]
        if not live:
            log.append(f"DRY-RUN {label}: would move for {rid} — run with --live to execute")
            return
        result = fn()
        stamp = f"{_now()} {label}: " + ("ok" if result.ok else f"FAILED — {result.error}")
        fields = {"Worker Log": (rec['fields'].get('Worker Log') or '') + stamp + "\n"}
        if result.ok:
            fields.update(ok_fields)
        _patch(lines, rid, fields)
        log.append(stamp)

    def each(status: str, handle) -> None:
        # One bad row logs and moves on; it must never stall every other item behind it.
        for rec in _by_status(lines, status):
            try:
                handle(rec, rec["fields"])
            except Exception as exc:
                log.append(f"{status} row {rec['id']} failed: {exc}")

    # 1. Stock may have moved since the form: re-check open asks against the fresh count.
    def validate(rec: dict, f: dict) -> None:
        stock = _stock_for(inventory, f)
        qty = _qty(f)
        if stock is None:
            _patch(lines, rec["id"], {"Worker Log": f"{_now()} item not found in Pull Inventory — pick it from the list\n"})
            log.append(f"unknown item on {rec['id']}: {f.get('Item')!r}")
            return
        available = int(stock.get("Available") or 0)
        fields: dict[str, Any] = {}
        if not (f.get("Request") or "").strip(" /"):
            fields["Request"] = f"{_item_name(f, stock)} / {f.get('Requester Email', '?')}"[:250]
        if qty > available:
            blocked.add(rec["id"])
            fields.update({"Status": "Insufficient stock",
                           "Worker Log": (f.get("Worker Log") or "") +
                           f"{_now()} requested {qty}, only {available} available\n"})
            log.append(f"insufficient stock: {_item_name(f, stock)} x{qty} (available {available})")
        if fields:
            _patch(lines, rec["id"], fields)

    each("Requested", validate)
    each("Approved", validate)

    # 2. Approved -> reserve in Shopify.
    def reserve(rec: dict, f: dict) -> None:
        if rec["id"] in blocked:
            return
        stock = _stock_for(inventory, f)
        if not stock or not stock.get("Inventory Item ID"):
            log.append(f"cannot reserve {rec['id']}: no inventory id"); return
        qty = _qty(f)
        url = _record_url(base, table_id, rec["id"])
        act("reserve", rec,
            lambda: shop.reserve(stock["Inventory Item ID"], location, qty, url,
                                 expected_available=int(stock.get("Available") or 0)),
            {"Status": "Reserved", "Reserved At": _now()})

    each("Approved", reserve)

    # 3. Return accepted -> release; Write-off -> damaged. Both end Closed.
    def close_out(label: str, fn_name: str):
        def handle(rec: dict, f: dict) -> None:
            if f.get("Closed At"):
                return
            stock = _stock_for(inventory, f)
            if not stock or not stock.get("Inventory Item ID"):
                log.append(f"cannot {label} {rec['id']}: no inventory id"); return
            qty = _qty(f)
            url = _record_url(base, table_id, rec["id"])
            fn = getattr(shop, fn_name)
            act(label, rec, lambda: fn(stock["Inventory Item ID"], location, qty, url),
                {"Closed At": _now(), "Status": "Closed"})
        return handle

    each("Return accepted", close_out("release", "release"))
    each("Write-off", close_out("write_off", "write_off"))
    return log


# ---- --flow --------------------------------------------------------------------------------

def _describe_item(f: dict) -> str:
    parts = []
    for key in ("Style", "Color", "Size"):
        v = f.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            parts.append(str(v))
    return " / ".join(parts) if parts else _item_name(f, None)


def flow(lines: Airtable, orders: Airtable) -> list[str]:
    data = lines._call("GET", lines.table, params={"pageSize": 100})
    groups: dict[str, list[dict]] = defaultdict(list)
    for rec in data.get("records", []):
        order = rec["fields"].get("Order")
        groups[order[0] if isinstance(order, list) and order else ""].append(rec)
    numbers = {r["id"]: r["fields"].get("Pull #") for r in _fetch(orders, "TRUE()")}
    out = []
    for key, recs in sorted(groups.items(), key=lambda kv: (kv[0] == "", numbers.get(kv[0], 0))):
        f0 = recs[0]["fields"]
        label = f"PULL #{numbers.get(key, '?')}" if key else "PRE-REDESIGN ROWS (no pull)"
        out.append(f"{label}  ·  {f0.get('Requester Email', '?')}  ·  return {f0.get('Expected Return Date', '?')}")
        for rec in recs:
            f = rec["fields"]
            status = f.get("Status") or "(blank)"
            step, where, nxt = STAGES.get(status, (0, f"status {status!r} — not part of the flow", "a human"))
            decided = f.get("Decided By")
            who = decided.get("email") if isinstance(decided, dict) else None
            avail = f.get("Available"); avail = avail[0] if isinstance(avail, list) and avail else avail
            tag = f.get("Overdue") or ""
            out.append(
                f"   {'step ' + str(step) if step else 'closed'}  {_describe_item(f)}  x{_qty(f)}"
                f"  (available now {avail if avail is not None else '?'})  {tag}\n"
                f"        status: {status} — {where}" + (f"  (decided by {who})" if who else "") + "\n"
                f"        next:   {nxt}")
        out.append("")
    return out or ["no requests yet — submit the form to start one"]


FLOW_EXPLAINER = """THE FLOW (each step, who does it, and what to expect)

  step 1  HUMAN  requester submits the pull form (up to 5 items, one reason, one return date)
          AGENT  creates one row per item in Pull Requests against fresh stock; short asks are
                 filed as Insufficient stock
          AUTO   one email to Sarena + Lillian listing every item with its live count;
                 one confirmation to the requester with the return-form link;
                 one short-stock email to the requester if anything was short
  step 2  HUMAN  Sarena or Lillian set each item Approved (or Denied -> requester emailed)
          AGENT  `--once --live` reserves the units in Shopify -> Reserved
  step 3  the pieces are out.  AUTO: return-day reminder to the requester (with the form);
          if overdue: OVERDUE tag on the item, daily email to the requester (all items on the
          pull) and to Sarena + Lillian (every overdue item)
  step 4  HUMAN  requester answers the return form (Yes/No + condition)
          AGENT  Yes -> the pull's reserved items become Return submitted
          AUTO   Sarena + Lillian emailed the response
  step 5  HUMAN  Sarena or Lillian set the item Return accepted (or Write-off)
          AGENT  `--once --live` releases the hold (or moves to damaged) -> Closed

  `--once` alone is a dry run: it prints what it WOULD do and moves nothing.
"""


def run_once(shop: PortalShopify, tables: dict[str, Airtable], base: str, live: bool) -> list[str]:
    """One full pass: sync stock, fan out new pulls, process return forms, execute moves."""
    orders, lines, returns, inventory = (tables[ORDERS_TABLE], tables[REQUESTS_TABLE],
                                         tables[RETURNS_TABLE], tables[INVENTORY_TABLE])
    out = [f"inventory synced: {sync_inventory(shop, inventory)} items"]
    out += ["  " + l for l in fan_out(orders, lines, inventory)]
    out += ["  " + l for l in process_returns(returns, orders, lines)]
    log = process(shop, lines, inventory, base, lines.create_table(), live=live)
    out += ["  " + l for l in log]
    if live and any(": ok" in l for l in log):
        sync_inventory(shop, inventory)
        out.append("inventory re-synced after moves")
    if not live:
        out.append("(dry-run — no inventory was moved; add --live to execute)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="aflalo-pulls")
    ap.add_argument("--setup", action="store_true", help="create/upgrade the four Airtable tables")
    ap.add_argument("--flow", action="store_true", help="show where every pull stands and what happens next")
    ap.add_argument("--once", action="store_true", help="one pass: fan out, validate, sync, execute moves")
    ap.add_argument("--live", action="store_true", help="actually move inventory (default: dry-run)")
    ap.add_argument("--return-form-url", metavar="URL", help="where the portal web pages are hosted (PORTAL_URL)")
    ap.add_argument("--draft-audit", action="store_true", help="list open draft orders (legacy)")
    args = ap.parse_args()

    from . import config  # noqa: F401  (loads .env before the environment is read)

    token, base = os.environ.get("AIRTABLE_TOKEN"), os.environ.get("PULLS_AIRTABLE_BASE")
    if not (token and base):
        print("Set AIRTABLE_TOKEN and PULLS_AIRTABLE_BASE (the portal lives in its own "
              "base, deliberately separate from the CS pipeline's)."); return 2
    if not (os.environ.get("PORTAL_SHOPIFY_API_KEY") and os.environ.get("PORTAL_SHOPIFY_API_SECRET")):
        print("The portal's own Shopify app isn't configured yet.\n"
              "Create app 'inventory-portal' (scopes: write_inventory, read_inventory, "
              "read_products, read_locations, read_draft_orders), install it, then set\n"
              "PORTAL_SHOPIFY_API_KEY / PORTAL_SHOPIFY_API_SECRET in .env")
        return 2

    tables = _tables(token, base)
    orders, lines, returns, inventory = (tables[ORDERS_TABLE], tables[REQUESTS_TABLE],
                                         tables[RETURNS_TABLE], tables[INVENTORY_TABLE])

    if args.setup:
        made = setup(token, base, tables)
        schema = _schema(token, base)
        print("tables ready:")
        for name in (ORDERS_TABLE, REQUESTS_TABLE, RETURNS_TABLE, INVENTORY_TABLE):
            print(f"  {name:15} https://airtable.com/{base}/{schema[name]['id']}")
        if made:
            print("  created:", ", ".join(made))
        print("Forms: `python -m aflalo_pulls.web` serves the request form at / and the return form\n"
              "at /return/<pull>. Set PORTAL_URL (or run `worker --return-form-url <url>`) so emails\n"
              "link to them.")
        return 0

    if args.return_form_url:
        set_return_form_url(token, base, args.return_form_url)
        print("every pull's 'Return form link' now points at <url>/return/<pull record>")
        return 0

    if args.flow:
        print(FLOW_EXPLAINER)
        print("OPEN PULLS RIGHT NOW\n")
        for line in flow(lines, orders):
            print(line)
        return 0

    shop = portal_client()

    if args.draft_audit:
        for d in shop.open_draft_orders():
            print(f"  {d['name']}  opened {d['created_at'][:10]}  {'; '.join(d['items'][:4])}")
        return 0

    if args.once:
        for line in run_once(shop, tables, base, live=args.live):
            print(line)
        return 0

    ap.print_help(); return 0


if __name__ == "__main__":
    raise SystemExit(main())
