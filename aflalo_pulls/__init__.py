"""Internal inventory pull request portal — Project 2.

Airtable is the surface (form, approvals, reminders — all no-code automations); this
package is the worker that makes the verified Shopify inventory moves. Decisions and API
evidence: portal/DECISIONS.md and portal/SCOPING.md.

Credential separation is the point: this package authenticates as its OWN Shopify app
(write_inventory), so the CS agent's app keeps zero write access, structurally.
"""
