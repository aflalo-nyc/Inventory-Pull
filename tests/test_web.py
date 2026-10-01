"""The portal's own forms — the validation that stands between a browser and Pull Orders."""

import pytest

from aflalo_pulls import web

KNOWN = {"recA", "recB"}


def _req(**over):
    base = {"passcode": "", "email": "ava@aflalonyc.com", "reason": "Photoshoot", "description": "Zoe Kravitz shoot, Oct 3",
            "return_date": "2026-10-10",
            "item1": "recA", "qty1": "2", "item2": "recB", "qty2": "1"}
    base.update(over)
    return base


def test_a_good_request_becomes_one_pull_orders_row_with_compacted_slots():
    fields = web.parse_request(_req(item1="", qty1="", item3="recA", qty3="3"), KNOWN)   # slot 1 blank, 2 and 3 used
    assert fields["Item 1"] == ["recB"] and fields["Qty 1"] == "1"       # gaps close up
    assert fields["Item 2"] == ["recA"] and fields["Qty 2"] == "3"
    assert "Item 3" not in fields
    assert fields["Requester Email"] == "ava@aflalonyc.com" and fields["Reason"] == "Photoshoot"
    assert fields["Description"] == "Zoe Kravitz shoot, Oct 3"


@pytest.mark.parametrize("bad, msg", [
    (dict(email="ava@gmail.com"), "@aflalonyc.com"),
    (dict(reason="Because"), "reason"),
    (dict(return_date=""), "return date"),
    (dict(description=""), "which shoot"),
    (dict(item1="recZZZ"), "pick it from the list"),
    (dict(qty1="9"), "quantity"),
    (dict(item2="recA"), "same piece"),
    (dict(item1="", qty1="", item2="", qty2=""), "at least one item"),
])
def test_bad_requests_are_bounced_with_a_human_reason(bad, msg):
    with pytest.raises(web.Rejected) as e:
        web.parse_request(_req(**bad), KNOWN)
    assert msg in str(e.value)


def test_passcode_is_enforced_when_configured(monkeypatch):
    monkeypatch.setenv("PORTAL_PASSCODE", "shh")
    with pytest.raises(web.Rejected, match="passcode"):
        web.parse_request(_req(passcode="nope"), KNOWN)
    assert web.parse_request(_req(passcode="shh"), KNOWN)["Item 1"] == ["recA"]


def test_return_form_requires_a_pull_and_a_reason_when_not_back():
    ok = web.parse_return({"pull": "recX", "returned": "Yes", "condition": ""})
    assert ok == {"Pull": ["recX"], "Returned?": "Yes", "Return condition": ""}
    with pytest.raises(web.Rejected, match="link in your email"):
        web.parse_return({"pull": "", "returned": "Yes"})
    with pytest.raises(web.Rejected, match="where it is"):
        web.parse_return({"pull": "recX", "returned": "No", "condition": ""})


def test_pages_render_without_a_server():
    assert "Pull request" in web.request_form() and "/inventory.json" in web.request_form()
    assert "Wrong passcode" in web.request_form("Wrong passcode.", {"email": "x@aflalonyc.com"})
    assert "Return pull #7" in web.return_form("recX", {"Pull #": 7, "Items": "• thing x1", "Requester Email": "a@b", "Expected Return Date": "2026-10-10"})
