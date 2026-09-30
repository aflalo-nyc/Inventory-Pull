"""A small Airtable REST client: create/upgrade a table from a schema, upsert rows by a key.

Schema entries are {"name", "type", "note", and for selects "options": [...], for numbers
"precision"}. Anything fancier (links, lookups, formulas) is created by the worker's setup()
with the Meta API directly, because those need other tables' ids.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

API = "https://api.airtable.com/v0"


def _field_defs(schema: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate a schema into Airtable Meta-API field definitions."""
    out: list[dict[str, Any]] = []
    for f in schema:
        d: dict[str, Any] = {"name": f["name"], "type": f["type"]}
        if f.get("note"):
            d["description"] = f["note"][:400]
        if f["type"] in ("singleSelect", "multipleSelects"):
            d["options"] = {"choices": [{"name": c} for c in f.get("options") or []]}
        elif f["type"] == "dateTime":
            d["options"] = {"timeZone": "client", "dateFormat": {"name": "iso"}, "timeFormat": {"name": "24hour"}}
        elif f["type"] == "date":
            d["options"] = {"dateFormat": {"name": "iso"}}
        elif f["type"] == "number":
            d["options"] = {"precision": f.get("precision", 2)}
        elif f["type"] == "checkbox":
            d["options"] = {"icon": "check", "color": "greenBright"}
        out.append(d)
    return out


class Airtable:
    def __init__(self, token: str, base: str, table: str, schema: list[dict[str, Any]],
                 key_field: str, description: str = "") -> None:
        self.token, self.base, self.table = token, base, table
        self.schema, self.key_field, self.description = schema, key_field, description

    def _meta(self, method: str, path: str, payload: dict | None = None) -> dict:
        req = urllib.request.Request(
            f"{API}/meta/bases/{self.base}/{path}" if path else f"{API}/meta/bases/{self.base}",
            data=json.dumps(payload).encode() if payload else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Airtable {exc.code}: {exc.read()[:400].decode()}") from exc

    def list_tables(self) -> list[dict]:
        return self._meta("GET", "tables").get("tables", [])

    def create_table(self) -> str:
        """Idempotent: returns the id if the table already exists."""
        for t in self.list_tables():
            if t["name"].lower() == self.table.lower():
                return t["id"]
        created = self._meta("POST", "tables", {"name": self.table, "description": self.description,
                                                 "fields": _field_defs(self.schema)})
        return created["id"]

    def ensure_fields(self, table_id: str) -> list[str]:
        """Add any schema field the table is missing. Returns the names added."""
        existing = {f["name"] for t in self.list_tables() if t["id"] == table_id for f in t.get("fields", [])}
        added = []
        for d in _field_defs(self.schema):
            if d["name"] in existing:
                continue
            self._meta("POST", f"tables/{table_id}/fields", d)
            added.append(d["name"])
        return added

    def _url(self, path: str, params: dict | None = None) -> str:
        # Encode each path segment separately: spaces in table names must become %20, but
        # the '/' between table and record id must survive (quoting it turns
        # "Pull Requests/rec123" into one nonexistent table name and Airtable 403s).
        quoted = "/".join(urllib.parse.quote(seg, safe="") for seg in path.split("/"))
        url = f"{API}/{self.base}/{quoted}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        return url

    def _call(self, method: str, path: str, payload: dict | None = None, params: dict | None = None) -> dict:
        req = urllib.request.Request(
            self._url(path, params),
            data=json.dumps(payload).encode() if payload else None,
            headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Airtable {exc.code}: {exc.read()[:300].decode()}") from exc

    def existing_ids(self) -> dict[str, str]:
        """Map key field -> record id, so a re-push updates instead of duplicating."""
        out, offset = {}, None
        while True:
            params: dict = {"fields[]": self.key_field, "pageSize": 100}
            if offset:
                params["offset"] = offset
            data = self._call("GET", self.table, params=params)
            for rec in data.get("records", []):
                key = rec.get("fields", {}).get(self.key_field)
                if key:
                    out[key] = rec["id"]
            offset = data.get("offset")
            if not offset:
                return out

    def push(self, rows: list[dict], row_fn=lambda r: r) -> tuple[int, int]:
        known = self.existing_ids()
        mapped = [row_fn(r) for r in rows]
        create = [{"fields": f} for f in mapped if f[self.key_field] not in known]
        update = [{"id": known[f[self.key_field]], "fields": f} for f in mapped if f[self.key_field] in known]
        for batch in (create[i:i + 10] for i in range(0, len(create), 10)):
            self._call("POST", self.table, {"records": batch, "typecast": True})
        for batch in (update[i:i + 10] for i in range(0, len(update), 10)):
            self._call("PATCH", self.table, {"records": batch, "typecast": True})
        return len(create), len(update)
