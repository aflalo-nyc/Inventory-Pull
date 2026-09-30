"""Shopify Admin GraphQL plumbing: client-credentials token mint (24h, re-minted on 401 once)
and a POST helper. Same code the CS agent uses; the portal keeps its own copy so the two
projects share no files. Scopes here are the PORTAL app's."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

TOKEN_REFRESH_BUFFER_S = 60

# Shopify answers a failed token exchange with an HTML error page, and the useful part is
# one word in its <title>. Dumping the raw page into a routing reason makes the audit trail
# unreadable and tells whoever is looking at it nothing about what to fix.
OAUTH_ERROR_RE = re.compile(r"Oauth error (\w+)|\"error\":\s*\"([^\"]+)\"")

OAUTH_ERROR_HELP = {
    "app_not_installed": (
        "the app is recognised but is not installed on this store — "
        "Dev Dashboard -> your app -> Home -> Install app -> pick the store"
    ),
    "application_cannot_be_found": (
        "SHOPIFY_API_KEY matches no app in this store's organization — "
        "check the client id, and that app and store are in the same org"
    ),
    "invalid_request": "SHOPIFY_API_SECRET was rejected — check the client secret",
    "invalid_client": "SHOPIFY_API_SECRET was rejected — check the client secret",
}


# read_orders and read_customers are protected customer data scopes: granting them in the
# Dev Dashboard is not enough on its own, they also need the protected customer data request
# completed and the app reinstalled. That distinction is the difference between "I ticked the
# box" and "it works", so name it in the error.
REQUIRED_SCOPES = ("write_inventory", "read_inventory", "read_products", "read_locations", "read_draft_orders")
PROTECTED_SCOPES: tuple[str, ...] = ()

PROBE_QUERY = (
    "{ currentAppInstallation { app { title } accessScopes { handle } } "
    "shop { name currencyCode } }"
)


def _explain_graphql_errors(errors: list[dict[str, Any]]) -> str:
    """An access denial is a missing scope, not a malformed query. Say so."""
    messages = [str(e.get("message", "")).strip() for e in errors]
    denied = [m for m in messages if "access denied" in m.lower()]
    if denied:
        return (
            f"Shopify access denied ({denied[0]}) — the token has no read scope. Grant "
            f"{', '.join(REQUIRED_SCOPES)} in the Dev Dashboard and reinstall; "
            f"{' and '.join(PROTECTED_SCOPES)} also need protected customer data approval."
        )
    return f"Shopify GraphQL error: {'; '.join(messages)[:200]}"


def _explain_oauth_error(body: str) -> str:
    """Turn Shopify's HTML error page into the one line worth acting on."""
    m = OAUTH_ERROR_RE.search(body)
    if not m:
        return body[:200].strip()
    code = m.group(1) or m.group(2)
    help_text = OAUTH_ERROR_HELP.get(code)
    return f"{code} — {help_text}" if help_text else code


def mint_access_token(
    shop_domain: str, client_id: str, client_secret: str, timeout: float = 10.0
) -> tuple[str, float]:
    """Exchange the app's client id + secret for an Admin API access token.

    The client credentials grant, which is the whole point: an app that only ever touches
    stores in its own Shopify organization does not need a merchant to click through OAuth.
    It works only when the app and the store are in the same organization.

    Returns (token, expires_at_epoch_seconds).
    """
    body = urllib.parse.urlencode(
        {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode()
    req = urllib.request.Request(
        f"https://{shop_domain}/admin/oauth/access_token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = _explain_oauth_error(exc.read().decode("utf-8", "replace"))
        raise RuntimeError(f"Shopify token exchange HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise RuntimeError(f"Shopify token endpoint unreachable: {exc}") from exc

    token = payload.get("access_token")
    if not token:
        raise RuntimeError(f"Shopify token exchange returned no access_token: {payload}")
    return token, time.time() + float(payload.get("expires_in", 86399))


@dataclass
class ShopifyAdminClient:
    """Live Admin GraphQL client. Scopes: the portal app's (REQUIRED_SCOPES).

    Authenticates one of two ways, and it does not care which:

    - `access_token` — a long-lived `shpat_` token from an admin-created custom app.
    - `client_id` + `client_secret` — the Dev Dashboard app's credentials, exchanged for a
      24-hour token on first use and re-minted automatically when it ages out.
    """

    shop_domain: str
    access_token: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    api_version: str = "2026-01"
    timeout: float = 10.0
    _token: str | None = field(default=None, init=False, repr=False)
    _expires_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        if not (self.access_token or (self.client_id and self.client_secret)):
            raise ValueError(
                "ShopifyAdminClient needs either access_token, or client_id + client_secret"
            )

    @property
    def mints_its_own_token(self) -> bool:
        return not self.access_token

    def _bearer(self, force_refresh: bool = False) -> str:
        """The value for X-Shopify-Access-Token, minted and cached as needed."""
        if self.access_token:
            return self.access_token
        if force_refresh or not self._token or time.time() >= self._expires_at - TOKEN_REFRESH_BUFFER_S:
            self._token, self._expires_at = mint_access_token(
                self.shop_domain, self.client_id or "", self.client_secret or "", self.timeout
            )
        return self._token

    @property
    def endpoint(self) -> str:
        return f"https://{self.shop_domain}/admin/api/{self.api_version}/graphql.json"

    def _graphql(self, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
        """POST a query with a minted token. Raises on transport errors."""
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()

        def call(token: str) -> dict[str, Any]:
            req = urllib.request.Request(
                self.endpoint,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "X-Shopify-Access-Token": token,
                },
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())

        try:
            return call(self._bearer())
        except urllib.error.HTTPError as exc:
            # A revoked or rotated token reads as a 401. Worth exactly one re-mint —
            # retrying further would just hammer the token endpoint with bad credentials.
            if exc.code != 401 or not self.mints_its_own_token:
                raise
            return call(self._bearer(force_refresh=True))


    def probe(self) -> str:
        """Mint a token and report which app it is and what it can reach."""
        try:
            body = self._graphql(PROBE_QUERY)
        except urllib.error.HTTPError as exc:
            return f"HTTP {exc.code}"
        except (OSError, RuntimeError) as exc:
            return str(exc)
        if body.get("errors"):
            return _explain_graphql_errors(body["errors"])
        data = body.get("data") or {}
        install = data.get("currentAppInstallation") or {}
        app = (install.get("app") or {}).get("title", "?")
        granted = {s["handle"] for s in install.get("accessScopes", [])}
        missing = [s for s in REQUIRED_SCOPES if s not in granted]
        where = f"app '{app}' on '{(data.get('shop') or {}).get('name', '?')}'"
        return f"{where} — missing scopes: {', '.join(missing)}" if missing else f"{where} — all {len(REQUIRED_SCOPES)} portal scopes present"
