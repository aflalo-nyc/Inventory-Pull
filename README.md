# AFLALO Inventory Pulls

Internal inventory pull requests, without draft orders. A requester fills one Airtable form
(up to five items, each with its own quantity); Sarena or Lillian approve or deny each item;
approved units are reserved in Shopify so the website can't sell them while they're out;
the requester answers a return form when the pieces come back; an approver confirms and the
units go back on sale. Every email is an Airtable automation. This code is two things: the **forms** (our own web
pages — Airtable's form builder can't do several items with quantities) and the **worker** in
the middle, which turns forms into item rows, checks stock, and moves inventory in Shopify.

It has no AI in it. It is a fixed sequence that runs every few minutes.

## Run

    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env        # fill in the five values
    .venv/bin/python -m aflalo_pulls.worker --setup          # create/upgrade the four tables
    .venv/bin/python -m aflalo_pulls.worker --flow           # where every pull stands
    .venv/bin/python -m aflalo_pulls.worker --once           # one pass, DRY RUN (default)
    .venv/bin/python -m aflalo_pulls.worker --once --live    # one pass, moving inventory
    .venv/bin/python -m aflalo_pulls.web                     # the forms on :8080 (+ worker on a timer)
    .venv/bin/pytest -q

## Deploy (Railway)

`railway.json` declares the whole deploy: one web service running `python -m aflalo_pulls.web`.
That process serves the two forms AND runs the worker pass every `WORKER_INTERVAL_MIN` minutes
(default 5) — nothing else to schedule.

1. Railway → New Project → Deploy from GitHub → this repo. Railway reads `railway.json`.
2. Service → Settings → Networking → Generate Domain. Copy it: that is `PORTAL_URL`.
3. Service → Variables → every key in `.env.example` (Railway injects them as the environment;
   there is no `.env` on the server). Set `WORKER_LIVE=1` once the supervised test has passed.
4. Deploy. The log's first lines are `portal forms on …` and `worker: every 5 min, LIVE`.
5. Once: `python -m aflalo_pulls.worker --return-form-url https://<PORTAL_URL>` so every pull's
   email links to its return page. Share `https://<PORTAL_URL>/` with the team as the request form.

Cost: one small always-on service, a few dollars a month.

## Docs

- `docs/PORTAL.html` — the project page (what it is, the workflow, the forms, the tech, handoff).
  Also published for the team at https://claude.ai/artifact/UMP36SzgyUwPCegEXcKFNp
- `docs/DECISIONS.md` — every design decision and its reason, including the 2026-09-14
  redesign (multi-item pulls, return form, open/closed/overdue, grouped emails).
- `docs/SCOPING.md` — the Shopify API feasibility check, with evidence.
- `docs/TESTING.md` — the supervised live test, step by step (Human / Automatic / Agent).
- `docs/QUESTIONS.md` — the original scoping questions and answers.

## Layout

    aflalo_pulls/web.py                the two forms (request at /, return at /return/<pull>) + worker timer
    aflalo_pulls/worker.py             the worker: setup, fan-out, returns, reserve/release, --flow
    aflalo_pulls/shopify_inventory.py  the verified inventory moves (reserve / release / write-off)
    aflalo_pulls/shopify_client.py     token mint + GraphQL POST
    aflalo_pulls/airtable.py           small Airtable REST client
    aflalo_pulls/config.py             .env loader
    tests/test_pulls.py                the decision logic, with Shopify and Airtable faked
