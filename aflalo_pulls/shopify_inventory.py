"""The portal's Shopify client: the CS client's plumbing plus the inventory moves.

Every mutation here was verified against the live store schema before this was written
(portal/SCOPING.md): inventoryMoveQuantities with reasons reservation_created /
reservation_deleted / damaged, mandatory ledgerDocumentUri for non-available states, and
changeFromQuantity as the race guard.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from .shopify_client import ShopifyAdminClient

MOVE_MUTATION = """
mutation Move($input: InventoryMoveQuantitiesInput!) {
  inventoryMoveQuantities(input: $input) {
    inventoryAdjustmentGroup { reason changes { name delta } }
    userErrors { field message }
  }
}
"""

LOCATIONS_QUERY = "{ locations(first: 5, includeInactive: false) { edges { node { id name } } } }"

OPEN_DRAFT_ORDERS_QUERY = """
{ draftOrders(first: 50, query: "status:open") {
    edges { node { id name createdAt totalPriceSet { shopMoney { amount } }
      lineItems(first: 10) { edges { node { title quantity } } } } }
} }
"""


def portal_client() -> "PortalShopify":
    """The portal's OWN app credentials — never the CS agent's."""
    return PortalShopify(
        shop_domain=os.environ["SHOPIFY_SHOP_DOMAIN"],
        client_id=os.environ["PORTAL_SHOPIFY_API_KEY"],
        client_secret=os.environ["PORTAL_SHOPIFY_API_SECRET"],
    )


@dataclass
class MoveResult:
    ok: bool
    error: str | None = None


class PortalShopify(ShopifyAdminClient):
    """Read plumbing inherited; adds the three verified inventory moves."""

    def single_location_id(self) -> str:
        """Answer 1 said pulls come from 'Shopify NYC'. The store turned out to have THREE
        active locations (NYC, SAMPLE SALE, Wholesale — discovered live 2026-09-03), so we
        pin to the named pull location instead of assuming one. Override with
        PORTAL_LOCATION_NAME if the pull source ever changes."""
        want = os.environ.get("PORTAL_LOCATION_NAME", "NYC").strip().lower()
        body = self._graphql(LOCATIONS_QUERY)
        if body.get("errors"):
            raise RuntimeError(f"locations query failed: {body['errors'][0].get('message', '')[:120]}")
        edges = body["data"]["locations"]["edges"]
        for e in edges:
            if e["node"]["name"].strip().lower() == want:
                return e["node"]["id"]
        names = [e["node"]["name"] for e in edges]
        raise RuntimeError(
            f"no active location named {want!r} — found {names}. Set PORTAL_LOCATION_NAME "
            "to one of these exact names"
        )

    def _move(self, *, item_id: str, location_id: str, qty: int, reason: str,
              from_name: str, to_name: str, ledger_uri: str, reference_uri: str,
              expected_from: int | None = None) -> MoveResult:
        terminal_from: dict[str, Any] = {"locationId": location_id, "name": from_name}
        terminal_to: dict[str, Any] = {"locationId": location_id, "name": to_name}
        # ledgerDocumentUri is REQUIRED whenever the state isn't 'available' (docs) —
        # it is also the whole audit story: every unit points at its pull request.
        if from_name != "available":
            terminal_from["ledgerDocumentUri"] = ledger_uri
        if to_name != "available":
            terminal_to["ledgerDocumentUri"] = ledger_uri
        if expected_from is not None:
            terminal_from["changeFromQuantity"] = expected_from

        body = self._graphql_vars(MOVE_MUTATION, {"input": {
            "reason": reason,
            "referenceDocumentUri": reference_uri,
            "changes": [{"inventoryItemId": item_id, "quantity": qty,
                         "from": terminal_from, "to": terminal_to}],
        }})
        if body.get("errors"):
            return MoveResult(False, str(body["errors"][0].get("message", ""))[:200])
        errs = body["data"]["inventoryMoveQuantities"]["userErrors"]
        if errs:
            return MoveResult(False, "; ".join(e["message"] for e in errs)[:200])
        return MoveResult(True)

    def _graphql_vars(self, query: str, variables: dict) -> dict:
        import json as _json
        import urllib.request

        def call(token: str) -> dict:
            req = urllib.request.Request(
                self.endpoint,
                data=_json.dumps({"query": query, "variables": variables}).encode(),
                headers={"Content-Type": "application/json",
                         "X-Shopify-Access-Token": token},
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return _json.loads(r.read())

        return call(self._bearer())

    def reserve(self, item_id: str, location_id: str, qty: int,
                request_url: str, expected_available: int) -> MoveResult:
        """Approval: available -> reserved. changeFromQuantity makes two simultaneous
        approvals of the last unit impossible — the second fails instead of double-booking."""
        return self._move(item_id=item_id, location_id=location_id, qty=qty,
                          reason="reservation_created", from_name="available",
                          to_name="reserved", ledger_uri=request_url,
                          reference_uri=request_url, expected_from=expected_available)

    def release(self, item_id: str, location_id: str, qty: int, request_url: str) -> MoveResult:
        """Return: reserved -> available."""
        return self._move(item_id=item_id, location_id=location_id, qty=qty,
                          reason="reservation_deleted", from_name="reserved",
                          to_name="available", ledger_uri=request_url,
                          reference_uri=request_url)

    def write_off(self, item_id: str, location_id: str, qty: int, request_url: str) -> MoveResult:
        """Never coming back (answer 10): reserved -> damaged, Shopify's unavailable bucket
        for stock that won't sell. Counted on-hand, never sellable, ledger names the pull."""
        return self._move(item_id=item_id, location_id=location_id, qty=qty,
                          reason="damaged", from_name="reserved", to_name="damaged",
                          ledger_uri=request_url, reference_uri=request_url)

    def open_draft_orders(self) -> list[dict]:
        """Answer 11: the parallel-running legacy. Surfaced, never touched."""
        body = self._graphql(OPEN_DRAFT_ORDERS_QUERY)
        if body.get("errors"):
            raise RuntimeError(str(body["errors"][0].get("message", ""))[:150])
        out = []
        for e in body["data"]["draftOrders"]["edges"]:
            n = e["node"]
            items = [f"{i['node']['title']} x{i['node']['quantity']}"
                     for i in n["lineItems"]["edges"]]
            out.append({"name": n["name"], "created_at": n["createdAt"], "items": items})
        return out
