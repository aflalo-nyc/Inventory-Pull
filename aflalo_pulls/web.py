"""The portal's two forms, served by this code instead of Airtable's form builder.

    python -m aflalo_pulls.web          serve on $PORT (default 8080); also runs the worker
                                        every WORKER_INTERVAL_MIN minutes (default 5) when
                                        WORKER_LIVE=1 is set (dry-run otherwise)

  GET  /                   the pull request form: requester, reason, return date, up to five
                           items — each a searchable picker over Pull Inventory with its own
                           quantity and the live count shown as you pick
  POST /submit             writes ONE row to Pull Orders (the worker fans it into items)
  GET  /return/<record>    the return form for one pull, already knowing which pull it is
  POST /return/submit      writes ONE row to Pull Returns
  GET  /inventory.json     what the picker searches: active items with their live counts
  GET  /health

Why our own pages: Airtable's form builder is UI-only (no API, no connector), its forms can't
grow a list ("add another item"), and a form can't show the live count beside the pick. The
pages are plain HTML + a little JavaScript; nothing to build, no framework.

Access: internal tool. Every submission must carry the shared passcode (PORTAL_PASSCODE) and a
requester address at the company domain (PORTAL_EMAIL_DOMAIN, default aflalonyc.com).
"""

from __future__ import annotations

import html
import json
import os
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import config  # noqa: F401  (loads .env)
from .airtable import Airtable
from .worker import (INVENTORY_TABLE, ORDERS_TABLE, REASONS, RETURNS_TABLE, SLOTS, QTY_OPTIONS,
                     _create, _fetch, _get, _tables, run_once)

EMAIL_DOMAIN = os.environ.get("PORTAL_EMAIL_DOMAIN", "aflalonyc.com").lower()


class Rejected(ValueError):
    """A submission the page should bounce back to the person with a reason."""


# ---- validation (pure; tested) -----------------------------------------------------------------

def parse_request(form: dict[str, str], known_items: set[str]) -> dict[str, Any]:
    """Turn the posted form into the Pull Orders row, or raise Rejected with a human reason."""
    _check_passcode(form)
    email = form.get("email", "").strip().lower()
    if not email.endswith("@" + EMAIL_DOMAIN):
        raise Rejected(f"Use your @{EMAIL_DOMAIN} address.")
    reason = form.get("reason", "")
    if reason not in REASONS:
        raise Rejected("Pick a reason.")
    date = form.get("return_date", "")
    if not date or len(date) != 10:
        raise Rejected("Pick an expected return date.")
    fields: dict[str, Any] = {"Requester Email": email, "Reason": reason, "Expected Return Date": date}
    seen: set[str] = set()
    n = 0
    for i in range(1, SLOTS + 1):
        item, qty = form.get(f"item{i}", "").strip(), form.get(f"qty{i}", "").strip()
        if not item and not qty:
            continue
        if item not in known_items:
            raise Rejected(f"Item {i}: pick it from the list (it has to match a piece in stock).")
        if qty not in QTY_OPTIONS:
            raise Rejected(f"Item {i}: quantity must be 1 to {QTY_OPTIONS[-1]}.")
        if item in seen:
            raise Rejected(f"Item {i} is the same piece as an earlier line — change the quantity instead.")
        seen.add(item)
        n += 1
        fields[f"Item {n}"] = [item]
        fields[f"Qty {n}"] = qty
    if n == 0:
        raise Rejected("Add at least one item.")
    return fields


def parse_return(form: dict[str, str]) -> dict[str, Any]:
    _check_passcode(form)
    pull = form.get("pull", "").strip()
    if not pull.startswith("rec"):
        raise Rejected("This return page isn't tied to a pull. Open it from the link in your email.")
    answer = form.get("returned", "")
    if answer not in ("Yes", "No"):
        raise Rejected("Tell us: returned, yes or no?")
    notes = form.get("condition", "").strip()
    if answer == "No" and not notes:
        raise Rejected("If it's not back yet, say where it is or when it will be.")
    return {"Pull": [pull], "Returned?": answer, "Return condition": notes}


def _check_passcode(form: dict[str, str]) -> None:
    expected = os.environ.get("PORTAL_PASSCODE", "")
    if expected and form.get("passcode", "") != expected:
        raise Rejected("Wrong passcode.")


# ---- pages ----------------------------------------------------------------------------------------

STYLE = """
:root{--ink:#1d1b17;--muted:#6b665d;--line:#dedad2;--bg:#f7f6f3;--card:#fff;--accent:#2f5d50;--bad:#9c3b2e}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 -apple-system,"Helvetica Neue",Arial,sans-serif}
main{max-width:640px;margin:0 auto;padding:2rem 1.2rem 4rem}h1{font-size:1.5rem;margin:0 0 .3rem}.sub{color:var(--muted);margin:0 0 1.6rem}
label{display:block;font-size:.8rem;font-weight:600;letter-spacing:.04em;text-transform:uppercase;color:var(--muted);margin:1rem 0 .3rem}
input,select,textarea{width:100%;font:inherit;padding:.6rem .7rem;border:1px solid var(--line);border-radius:8px;background:var(--card)}
textarea{min-height:5rem}.item{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem;margin:.7rem 0;position:relative}
.item .row{display:grid;grid-template-columns:1fr 6.5rem;gap:.6rem}.item .avail{font-size:.85rem;color:var(--muted);margin-top:.4rem;min-height:1.2rem}
.item .avail.short{color:var(--bad);font-weight:600}.picks{position:absolute;left:1rem;right:7.5rem;background:var(--card);border:1px solid var(--line);border-radius:8px;
box-shadow:0 8px 24px rgba(0,0,0,.08);z-index:3;max-height:16rem;overflow:auto}.picks div{padding:.5rem .7rem;cursor:pointer;font-size:.93rem}
.picks div:hover{background:var(--bg)}.picks small{color:var(--muted)}.rm{position:absolute;top:.5rem;right:.6rem;background:none;border:0;color:var(--muted);font-size:1.1rem;cursor:pointer}
button.primary{margin-top:1.4rem;width:100%;padding:.85rem;font:inherit;font-weight:600;background:var(--accent);color:#fff;border:0;border-radius:8px;cursor:pointer}
button.ghost{margin-top:.4rem;width:100%;padding:.6rem;font:inherit;background:none;border:1px dashed var(--line);border-radius:8px;color:var(--muted);cursor:pointer}
.err{background:#f5e9e6;border-left:3px solid var(--bad);padding:.7rem 1rem;border-radius:8px;margin:1rem 0}
.ok{background:#eaf0ee;border-left:3px solid var(--accent);padding:1rem;border-radius:8px}pre{white-space:pre-wrap;font:inherit;margin:.5rem 0 0}
.radios{display:flex;gap:1.2rem}.radios label{display:flex;align-items:center;gap:.4rem;text-transform:none;font-size:1rem;font-weight:500;color:var(--ink);margin:.4rem 0}
.radios input{width:auto}
"""


def page(title: str, body: str) -> str:
    return (f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>{body}</main></body></html>")


def request_form(error: str = "", values: dict[str, str] | None = None) -> str:
    v = {k: html.escape(x) for k, x in (values or {}).items()}
    reasons = "".join(f"<option {'selected' if v.get('reason') == html.escape(r) else ''}>{html.escape(r)}</option>" for r in REASONS)
    qty = "".join(f"<option>{q}</option>" for q in QTY_OPTIONS)
    body = f"""
<h1>Pull request</h1>
<p class=sub>Up to {SLOTS} pieces from NYC stock. Sarena and Lillian approve each piece on its own; you'll get an email either way.</p>
{f'<div class=err>{html.escape(error)}</div>' if error else ''}
<form method=post action=/submit id=f>
<label>Your email</label><input name=email type=email required placeholder="you@{EMAIL_DOMAIN}" value="{v.get('email','')}">
<label>Reason</label><select name=reason required><option value="" disabled {'selected' if not v.get('reason') else ''}>Pick one</option>{reasons}</select>
<label>Expected return date</label><input name=return_date type=date required value="{v.get('return_date','')}">
<label>Items</label>
<div id=items></div>
<button type=button class=ghost id=add>+ add another piece</button>
<label>Passcode</label><input name=passcode type=password required placeholder="the team passcode">
<button class=primary>Submit pull request</button>
</form>
<template id=tpl>
<div class=item><button type=button class=rm title=remove>&times;</button>
<div class=row><input class=search placeholder="Search style, color, or size…" autocomplete=off><select class=qty>{qty}</select></div>
<input type=hidden class=id><div class=avail></div><div class=picks hidden></div></div>
</template>
<script>
const SLOTS={SLOTS}, qtyOpts={json.dumps(QTY_OPTIONS)};
let inv=[]; fetch('/inventory.json').then(r=>r.json()).then(d=>{{inv=d;}});
const items=document.getElementById('items'), tpl=document.getElementById('tpl'), add=document.getElementById('add');
function renumber(){{[...items.children].forEach((el,i)=>{{el.querySelector('.id').name='item'+(i+1); el.querySelector('.qty').name='qty'+(i+1);}}); add.hidden=items.children.length>=SLOTS;}}
function addRow(){{ if(items.children.length>=SLOTS) return; const el=tpl.content.firstElementChild.cloneNode(true); items.appendChild(el); wire(el); renumber(); }}
function wire(el){{
  const s=el.querySelector('.search'), id=el.querySelector('.id'), av=el.querySelector('.avail'), picks=el.querySelector('.picks'), q=el.querySelector('.qty');
  function show(){{ const t=s.value.trim().toLowerCase(); if(!t){{picks.hidden=true;return;}}
    const hits=inv.filter(x=>x.item.toLowerCase().includes(t)).slice(0,12);
    picks.innerHTML=hits.map(x=>`<div data-id="${{x.id}}" data-item="${{x.item.replace(/"/g,'&quot;')}}" data-av="${{x.available}}">${{x.item}} <small>· ${{x.available}} available</small></div>`).join('')||'<div><small>nothing matches</small></div>';
    picks.hidden=false; }}
  s.addEventListener('input',()=>{{id.value='';av.textContent='';show();}}); s.addEventListener('focus',show);
  picks.addEventListener('mousedown',e=>{{const d=e.target.closest('div[data-id]'); if(!d) return; id.value=d.dataset.id; s.value=d.dataset.item; picks.hidden=true; check();}});
  document.addEventListener('click',e=>{{ if(!el.contains(e.target)) picks.hidden=true; }});
  q.addEventListener('change',check);
  function check(){{ if(!id.value){{av.textContent='';return;}} const x=inv.find(y=>y.id===id.value); const n=+q.value;
    av.textContent=`${{x.available}} available at NYC`+(n>x.available?` — only ${{x.available}}, this line will be rejected`:''); av.classList.toggle('short',n>x.available); }}
  el.querySelector('.rm').addEventListener('click',()=>{{ if(items.children.length>1){{el.remove();renumber();}} }});
}}
add.addEventListener('click',addRow); addRow();
document.getElementById('f').addEventListener('submit',e=>{{ for(const el of items.children){{ if(!el.querySelector('.id').value){{ e.preventDefault(); el.querySelector('.search').focus(); el.querySelector('.avail').textContent='Pick this piece from the list.'; el.querySelector('.avail').classList.add('short'); return; }} }} }});
</script>"""
    return page("AFLALO pull request", body)


def request_done(pull_no: Any, items: str) -> str:
    return page("Pull request sent", f"""
<h1>Pull #{html.escape(str(pull_no))} received</h1>
<div class=ok>Sarena and Lillian have been told. You'll get a confirmation email with the return link,
and a separate note right away if anything you asked for is short on stock.<pre>{html.escape(items)}</pre></div>
<p><a href=/>Submit another</a></p>""")


def return_form(rec: str, order: dict[str, Any], error: str = "") -> str:
    items = html.escape((order.get("Items") or "").strip())
    body = f"""
<h1>Return pull #{html.escape(str(order.get('Pull #', '?')))}</h1>
<p class=sub>Requested by {html.escape(order.get('Requester Email', '?'))}, due back {html.escape(order.get('Expected Return Date', '?'))}.</p>
<pre>{items}</pre>
{f'<div class=err>{html.escape(error)}</div>' if error else ''}
<form method=post action=/return/submit>
<input type=hidden name=pull value="{html.escape(rec)}">
<label>Have the pieces been returned?</label>
<div class=radios><label><input type=radio name=returned value=Yes required> Yes, they're back</label><label><input type=radio name=returned value=No> Not yet</label></div>
<label>Condition / notes</label><textarea name=condition placeholder="Anything Sarena and Lillian should know: condition, missing tags, or when it will be back"></textarea>
<label>Passcode</label><input name=passcode type=password required placeholder="the team passcode">
<button class=primary>Send</button>
</form>"""
    return page("AFLALO pull return", body)


def return_done(answer: str) -> str:
    msg = ("Thanks. Sarena and Lillian have been told, and the pieces will be checked in and put back on sale."
           if answer == "Yes" else "Noted. Sarena and Lillian have been told the pieces are still out.")
    return page("Return noted", f"<h1>Got it</h1><div class=ok>{msg}</div>")


# ---- the server -----------------------------------------------------------------------------------

class Portal:
    def __init__(self) -> None:
        self.token, self.base = os.environ["AIRTABLE_TOKEN"], os.environ["PULLS_AIRTABLE_BASE"]
        self.tables = _tables(self.token, self.base)
        self._inv_cache: tuple[float, list[dict]] = (0.0, [])

    def inventory(self) -> list[dict]:
        """Active items with counts, cached for a minute — the picker fetches it once per page."""
        ts, data = self._inv_cache
        if time.time() - ts < 60 and data:
            return data
        recs = _fetch(self.tables[INVENTORY_TABLE], "{Active}")
        data = sorted(({"id": r["id"], "item": r["fields"].get("Item", ""),
                        "available": int(r["fields"].get("Available") or 0)} for r in recs),
                      key=lambda x: x["item"])
        self._inv_cache = (time.time(), data)
        return data

    def submit_request(self, form: dict[str, str]) -> tuple[Any, str]:
        fields = parse_request(form, {x["id"] for x in self.inventory()})
        rec = _create(self.tables[ORDERS_TABLE], fields)
        time.sleep(1.5)                                  # let the Items formula compute
        order = _get(self.tables[ORDERS_TABLE], rec)
        return order.get("Pull #", "?"), order.get("Items", "")

    def order(self, rec: str) -> dict[str, Any] | None:
        try:
            return _get(self.tables[ORDERS_TABLE], rec)
        except RuntimeError:
            return None

    def submit_return(self, form: dict[str, str]) -> str:
        fields = parse_return(form)
        _create(self.tables[RETURNS_TABLE], fields)
        return fields["Returned?"]


def make_handler(portal: Portal):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body: str, status: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
            data = body.encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _form(self) -> dict[str, str]:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode()
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

        def do_GET(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            try:
                if path == "/":
                    self._send(request_form())
                elif path == "/inventory.json":
                    self._send(json.dumps(portal.inventory()), ctype="application/json")
                elif path == "/health":
                    self._send("ok", ctype="text/plain")
                elif path.startswith("/return/"):
                    rec = path.rsplit("/", 1)[-1]
                    order = portal.order(rec) if rec.startswith("rec") else None
                    if not order:
                        self._send(page("Not found", "<h1>That pull doesn't exist</h1><p>Open the return link from your email.</p>"), 404)
                    else:
                        self._send(return_form(rec, order))
                else:
                    self._send(page("Not found", "<h1>Not found</h1>"), 404)
            except Exception:
                traceback.print_exc()
                self._send(page("Error", "<h1>Something went wrong</h1><p>Try again in a minute.</p>"), 500)

        def do_POST(self) -> None:
            path = urllib.parse.urlsplit(self.path).path
            form = self._form()
            try:
                if path == "/submit":
                    try:
                        pull_no, items = portal.submit_request(form)
                    except Rejected as exc:
                        self._send(request_form(str(exc), form), 400); return
                    self._send(request_done(pull_no, items))
                elif path == "/return/submit":
                    try:
                        answer = portal.submit_return(form)
                    except Rejected as exc:
                        order = portal.order(form.get("pull", "")) or {}
                        self._send(return_form(form.get("pull", ""), order, str(exc)), 400); return
                    self._send(return_done(answer))
                else:
                    self._send(page("Not found", "<h1>Not found</h1>"), 404)
            except Exception:
                traceback.print_exc()
                self._send(page("Error", "<h1>Something went wrong</h1><p>Try again in a minute.</p>"), 500)

        def log_message(self, fmt, *args):  # one line per request, no noise
            print(f"{self.address_string()} {fmt % args}", flush=True)

    return Handler


def worker_loop(portal: Portal) -> None:
    """The same pass as `worker --once`, on a timer, in this process."""
    from .shopify_inventory import portal_client
    live = os.environ.get("WORKER_LIVE") == "1"
    minutes = float(os.environ.get("WORKER_INTERVAL_MIN", "5"))
    shop = portal_client()
    print(f"worker: every {minutes:g} min, {'LIVE' if live else 'dry-run'}", flush=True)
    while True:
        try:
            for line in run_once(shop, portal.tables, portal.base, live=live):
                print("worker:", line, flush=True)
        except Exception:
            traceback.print_exc()
        time.sleep(minutes * 60)


def main() -> int:
    portal = Portal()
    if os.environ.get("WORKER_INTERVAL_MIN", "5") != "0":
        threading.Thread(target=worker_loop, args=(portal,), daemon=True).start()
    port = int(os.environ.get("PORT", "8080"))
    print(f"portal forms on http://0.0.0.0:{port}  (request form at /, return form at /return/<pull>)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(portal)).serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
