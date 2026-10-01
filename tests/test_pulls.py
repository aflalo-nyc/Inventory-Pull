"""Portal worker tests — the decision logic, with Shopify and Airtable faked."""

import pytest

from aflalo_pulls import worker
from aflalo_pulls.shopify_inventory import MoveResult


class FakeShop:
    def __init__(self):
        self.moves = []

    def single_location_id(self):
        return "gid://shopify/Location/1"

    def reserve(self, item_id, location, qty, url, expected_available):
        self.moves.append(("reserve", item_id, qty, url, expected_available))
        return MoveResult(True)

    def release(self, item_id, location, qty, url):
        self.moves.append(("release", item_id, qty, url))
        return MoveResult(True)

    def write_off(self, item_id, location, qty, url):
        self.moves.append(("write_off", item_id, qty, url))
        return MoveResult(True)


class FakeTable:
    """Enough of the Airtable REST surface for the worker: list with a formula, get by id,
    patch, create. Records are {"id", "fields"}; formulas are matched on a few shapes."""

    def __init__(self, records=None):
        self.records = {r["id"]: r for r in (records or [])}
        self.patches, self.created = [], []
        self.table = "t"
        self.counter = 0

    def _call(self, method, path, payload=None, params=None):
        if method == "PATCH":
            rid = path.split("/")[-1]
            self.patches.append((rid, payload["fields"]))
            self.records[rid]["fields"].update(payload["fields"])
            return {}
        if method == "POST":
            self.counter += 1
            rid = f"new{self.counter}"
            self.records[rid] = {"id": rid, "fields": dict(payload["records"][0]["fields"])}
            self.created.append(self.records[rid])
            return {"records": [{"id": rid}]}
        if "/" in path:
            return self.records.get(path.split("/")[-1], {})
        formula = (params or {}).get("filterByFormula", "")
        recs = list(self.records.values())
        if formula.startswith("{Status} = '"):
            want = formula.split("'")[1]
            recs = [r for r in recs if r["fields"].get("Status") == want]
        elif formula.startswith("NOT({"):
            field = formula[5:-2]
            recs = [r for r in recs if not r["fields"].get(field)]
        elif formula.startswith("{Item} = '"):
            recs = [r for r in recs if r["fields"].get("Item") == formula.split("'")[1]]
        return {"records": recs}


def _inv():
    return FakeTable([
        {"id": "invMira", "fields": {"Item": "Mira Jacket in Wool Silk / Olive / S", "Available": 3,
                                     "Inventory Item ID": "gid://shopify/InventoryItem/9"}},
        {"id": "invGide", "fields": {"Item": "Gide Sweater in Wool / Black / XS", "Available": 1,
                                     "Inventory Item ID": "gid://shopify/InventoryItem/10"}},
    ])


def _line(rid, status, item="invMira", qty="1", **extra):
    return {"id": rid, "fields": {"Status": status, "Item": [item], "Quantity": qty,
                                  "Requester Email": "jordyn@aflalonyc.com", **extra}}


# ---- fan-out: one form submission -> one row per item -------------------------------------

def test_a_two_item_form_becomes_two_lines_sharing_one_pull():
    orders = FakeTable([{"id": "ord1", "fields": {
        "Pull #": 7, "Requester Email": "jordyn@aflalonyc.com", "Reason": "Photoshoot",
        "Description": "Vogue shoot", "Expected Return Date": "2026-09-20",
        "Item 1": ["invMira"], "Qty 1": "2", "Item 2": ["invGide"], "Qty 2": "1"}}])
    lines = FakeTable()
    log = worker.fan_out(orders, lines, _inv())
    assert len(lines.created) == 2
    for l in lines.created:
        f = l["fields"]
        assert f["Order"] == ["ord1"] and f["Reason"] == "Photoshoot" and f["Description"] == "Vogue shoot"
        assert f["Expected Return Date"] == "2026-09-20" and f["Status"] == "Requested"
    assert {l["fields"]["Quantity"] for l in lines.created} == {"2", "1"}
    assert orders.records["ord1"]["fields"]["Lines created"] is True
    assert log == ["pull #7: 2 item(s) created, 0 short"]


def test_short_items_are_filed_as_insufficient_and_listed_once_on_the_pull():
    orders = FakeTable([{"id": "ord2", "fields": {
        "Pull #": 8, "Requester Email": "ava@aflalonyc.com",
        "Item 1": ["invMira"], "Qty 1": "1", "Item 2": ["invGide"], "Qty 2": "5"}}])
    lines = FakeTable()
    worker.fan_out(orders, lines, _inv())
    statuses = {l["fields"]["Item"][0]: l["fields"]["Status"] for l in lines.created}
    assert statuses == {"invMira": "Requested", "invGide": "Insufficient stock"}
    short = orders.records["ord2"]["fields"]["Short lines"]
    assert "Gide Sweater" in short and "asked 5, only 1" in short
    assert orders.records["ord2"]["fields"]["Lines created"] is True   # the good item still goes to approvers


def test_an_empty_form_creates_nothing_and_never_alerts_approvers():
    orders = FakeTable([{"id": "ord3", "fields": {"Pull #": 9, "Requester Email": "x@aflalonyc.com"}}])
    lines = FakeTable()
    worker.fan_out(orders, lines, _inv())
    assert lines.created == []
    f = orders.records["ord3"]["fields"]
    assert "Lines created" not in f and "No items" in f["Short lines"]


def test_fan_out_is_idempotent_once_lines_exist():
    orders = FakeTable([{"id": "ord4", "fields": {"Pull #": 10, "Lines created": True,
                                                   "Item 1": ["invMira"], "Qty 1": "1"}}])
    lines = FakeTable()
    worker.fan_out(orders, lines, _inv())
    assert lines.created == []


# ---- return form -----------------------------------------------------------------------------

def test_yes_on_the_return_form_moves_reserved_items_to_return_submitted():
    lines = FakeTable([_line("l1", "Reserved"), _line("l2", "Reserved", item="invGide"),
                       _line("l3", "Closed")])
    orders = FakeTable([{"id": "ord5", "fields": {"Pull #": 11, "Lines": ["l1", "l2", "l3"]}}])
    returns = FakeTable([{"id": "ret1", "fields": {"Pull": ["ord5"], "Returned?": "Yes",
                                                    "Return condition": "one button loose"}}])
    worker.process_returns(returns, orders, lines)
    assert lines.records["l1"]["fields"]["Status"] == "Return submitted"
    assert lines.records["l2"]["fields"]["Status"] == "Return submitted"
    assert lines.records["l3"]["fields"]["Status"] == "Closed"                   # untouched
    assert lines.records["l1"]["fields"]["Return condition"] == "one button loose"
    assert orders.records["ord5"]["fields"]["Return response"].startswith("Yes — one button loose")
    assert returns.records["ret1"]["fields"]["Processed"] is True


def test_no_on_the_return_form_only_records_the_response():
    lines = FakeTable([_line("l1", "Reserved")])
    orders = FakeTable([{"id": "ord6", "fields": {"Pull #": 12, "Lines": ["l1"]}}])
    returns = FakeTable([{"id": "ret2", "fields": {"Pull": ["ord6"], "Returned?": "No",
                                                    "Return condition": "still at the shoot, back Friday"}}])
    worker.process_returns(returns, orders, lines)
    assert lines.records["l1"]["fields"]["Status"] == "Reserved"
    assert "No — still at the shoot" in orders.records["ord6"]["fields"]["Return response"]
    assert returns.records["ret2"]["fields"]["Processed"] is True


# ---- moves -----------------------------------------------------------------------------------

def test_an_approval_reserves_with_the_race_guard_and_the_request_url():
    shop = FakeShop()
    lines = FakeTable([_line("r2", "Approved", qty="2")])
    worker.process(shop, lines, _inv(), "app1", "tbl1", live=True)
    kind, item_id, qty, url, expected = shop.moves[0]
    assert (kind, item_id, qty, expected) == ("reserve", "gid://shopify/InventoryItem/9", 2, 3)
    assert url.endswith("/r2")
    assert lines.records["r2"]["fields"]["Status"] == "Reserved"


def test_dry_run_is_the_default_and_moves_nothing():
    shop = FakeShop()
    lines = FakeTable([_line("r3", "Approved")])
    log = worker.process(shop, lines, _inv(), "app1", "tbl1", live=False)
    assert shop.moves == [] and any("DRY-RUN" in l for l in log)


def test_return_accepted_releases_and_write_off_damages_then_both_close():
    shop = FakeShop()
    lines = FakeTable([_line("r4", "Return accepted"), _line("r5", "Write-off"),
                       _line("r6", "Return accepted", **{"Closed At": "2026-09-01T00:00:00Z"})])
    worker.process(shop, lines, _inv(), "app1", "tbl1", live=True)
    assert [m[0] for m in shop.moves] == ["release", "write_off"]            # r6 untouched
    assert lines.records["r4"]["fields"]["Status"] == "Closed"
    assert lines.records["r5"]["fields"]["Status"] == "Closed"


def test_an_approved_over_ask_is_caught_before_the_reserve():
    shop = FakeShop()
    lines = FakeTable([_line("r7", "Approved", qty="4")])                   # only 3 available
    worker.process(shop, lines, _inv(), "app1", "tbl1", live=True)
    assert lines.records["r7"]["fields"]["Status"] == "Insufficient stock" and shop.moves == []


def test_one_bad_row_does_not_stall_the_rest_of_the_pass():
    shop = FakeShop()
    lines = FakeTable([_line("bad", "Requested", qty="lots"), _line("ok", "Requested", qty="5")])
    log = worker.process(shop, lines, _inv(), "app1", "tbl1", live=True)
    assert any("bad" in l and "failed" in l for l in log)
    assert lines.records["ok"]["fields"]["Status"] == "Insufficient stock"


# ---- misc --------------------------------------------------------------------------------------

def test_gift_cards_are_not_pullable():
    assert worker.is_gift_card({"title": "E-gift Card", "productType": "Gift card", "isGiftCard": True})
    assert worker.is_gift_card({"title": "Gift Card", "productType": "", "isGiftCard": False})
    assert not worker.is_gift_card({"title": "Mira Jacket in Wool Silk", "productType": "Outerwear", "isGiftCard": False})


def test_the_ledger_uri_is_never_a_shopify_gid():
    url = worker._record_url("appX", "tblY", "recZ")
    assert url.startswith("https://airtable.com/") and not url.startswith("gid://")


def test_airtable_urls_encode_spaces_but_keep_the_record_id_slash():
    from aflalo_pulls.airtable import Airtable
    t = Airtable.__new__(Airtable); t.base = "appX"
    url = t._url("Pull Requests/rec123", {"pageSize": 1})
    assert "Pull%20Requests/rec123?" in url and "%2F" not in url


def test_items_formula_covers_every_slot_and_flags_short():
    f = worker._items_formula()
    assert all(f"{{Item {n}}}" in f for n in range(1, worker.SLOTS + 1)) and "SHORT" in f
