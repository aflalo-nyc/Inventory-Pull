# AFLALO Inventory Pulls

Internal inventory pull requests, without draft orders. A requester fills one Airtable form
(up to five items, each with its own quantity); Sarena or Lillian approve or deny each item;
approved units are reserved in Shopify so the website can't sell them while they're out;
the requester answers a return form when the pieces come back; an approver confirms and the
units go back on sale. Every email is an Airtable automation. This code is the worker in
the middle: it turns forms into item rows, checks stock, and moves inventory in Shopify.

It has no AI in it. It is a fixed sequence that runs every few minutes.

## Run

    python -m venv .venv && .venv/bin/pip install -r requirements.txt
    cp .env.example .env        # fill in the five values
    .venv/bin/python -m aflalo_pulls.worker --setup          # create/upgrade the four tables
    .venv/bin/python -m aflalo_pulls.worker --flow           # where every pull stands
    .venv/bin/python -m aflalo_pulls.worker --once           # one pass, DRY RUN (default)
    .venv/bin/python -m aflalo_pulls.worker --once --live    # one pass, moving inventory
    .venv/bin/pytest -q

Deploy = run `--once --live` on a 5-minute schedule (a cron job; it exits in ~30s).

## Docs

- `docs/DECISIONS.md` — every design decision and its reason, including the 2026-09-14
  redesign (multi-item pulls, return form, open/closed/overdue, grouped emails).
- `docs/SCOPING.md` — the Shopify API feasibility check, with evidence.
- `docs/TESTING.md` — the supervised live test, step by step (Human / Automatic / Agent).
- `docs/QUESTIONS.md` — the original scoping questions and answers.

## Layout

    aflalo_pulls/worker.py             the worker: setup, fan-out, returns, reserve/release, --flow
    aflalo_pulls/shopify_inventory.py  the verified inventory moves (reserve / release / write-off)
    aflalo_pulls/shopify_client.py     token mint + GraphQL POST
    aflalo_pulls/airtable.py           small Airtable REST client
    aflalo_pulls/config.py             .env loader
    tests/test_pulls.py                the decision logic, with Shopify and Airtable faked
