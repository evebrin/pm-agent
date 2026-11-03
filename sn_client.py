"""Minimal ServiceNow REST Table API client.

Basic auth from .env, polite rate limiting, max 3 retries with backoff,
clear errors for a hibernating/unreachable instance, and a dry-run mode
that prints writes instead of sending them.
"""
import os
import sys
import time

import requests
from dotenv import load_dotenv

MAX_RETRIES = 3
MIN_INTERVAL = 0.2  # seconds between requests (polite to the PDI)
TIMEOUT = 30
RETRY_STATUS = {429, 500, 502, 503, 504}


class SNError(Exception):
    """Any ServiceNow API failure."""


class InstanceUnavailable(SNError):
    """Instance unreachable, hibernating, or still waking up."""


class AuthError(SNError):
    """401/403 from the instance."""


class SNClient:
    def __init__(self, dry_run=False, user_var="SN_USER", password_var="SN_PASSWORD"):
        load_dotenv()
        missing = [k for k in ("SN_INSTANCE", user_var, password_var) if not os.getenv(k)]
        if missing:
            raise SNError(f"Missing in .env: {', '.join(missing)} (see .env.example)")
        self.base = os.environ["SN_INSTANCE"].rstrip("/")
        self.dry_run = dry_run
        self.session = requests.Session()
        self.session.auth = (os.environ[user_var], os.environ[password_var])
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self._last = 0.0

    def _request(self, method, path, **kwargs):
        url = f"{self.base}{path}"
        for attempt in range(MAX_RETRIES + 1):
            wait = MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.request(method, url, timeout=TIMEOUT, allow_redirects=False, **kwargs)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == MAX_RETRIES:
                    raise InstanceUnavailable(
                        f"Cannot reach {self.base} ({type(e).__name__}). "
                        "Is the PDI hibernating? Open it in a browser to wake it, then retry."
                    ) from None
                time.sleep(2 ** attempt)
                continue

            if resp.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 401:
                raise AuthError(
                    f"HTTP 401 on {method} {path}: login failed. Check SN_INSTANCE points at the right PDI, "
                    "and SN_USER/SN_PASSWORD (a Web service access only user, password set by setup_integration_user.py)."
                )
            if resp.status_code == 403:
                raise SNError(f"HTTP 403 on {method} {path}: logged in, but this user's roles do not allow it.")
            # A hibernating PDI answers with a redirect or an HTML page instead of JSON.
            ctype = resp.headers.get("Content-Type", "")
            if 300 <= resp.status_code < 400 or (resp.status_code != 204 and "json" not in ctype):
                raise InstanceUnavailable(
                    f"{self.base} returned HTTP {resp.status_code} ({ctype or 'no content type'}) instead of JSON. "
                    "The PDI is probably hibernating or waking up; open it in a browser and retry in a minute."
                )
            if resp.status_code in RETRY_STATUS:
                raise InstanceUnavailable(f"HTTP {resp.status_code} after {MAX_RETRIES} retries on {method} {path}")
            if resp.status_code >= 400:
                raise SNError(f"HTTP {resp.status_code} on {method} {path}: {resp.text[:300]}")
            return resp.json() if resp.status_code != 204 else {}

    # --- reads -------------------------------------------------------------
    def get(self, table, query="", fields=None, limit=100, offset=0, display_value=False):
        # display_value=True returns labels/names, and journal fields (work_notes) as their history text
        params = {"sysparm_query": query, "sysparm_limit": limit, "sysparm_offset": offset,
                  "sysparm_exclude_reference_link": "true", "sysparm_display_value": str(display_value).lower()}
        if fields:
            params["sysparm_fields"] = ",".join(fields)
        return self._request("GET", f"/api/now/table/{table}", params=params)["result"]

    def get_all(self, table, query="", fields=None, page=500, display_value=False):
        rows, offset = [], 0
        while True:
            batch = self.get(table, query, fields, limit=page, offset=offset, display_value=display_value)
            rows.extend(batch)
            if len(batch) < page:
                return rows
            offset += page

    def count(self, table, query=""):
        params = {"sysparm_count": "true", "sysparm_query": query}
        return int(self._request("GET", f"/api/now/stats/{table}", params=params)["result"]["stats"]["count"])

    # --- writes (respect dry_run) -------------------------------------------
    # Writes also exclude reference links, so reference fields come back as plain sys_ids like reads.
    def create(self, table, data, params=None):
        if self.dry_run:
            print(f"[dry-run] CREATE {table}: {data}")
            return {"sys_id": "DRYRUN", **data}
        params = {"sysparm_exclude_reference_link": "true", **(params or {})}
        return self._request("POST", f"/api/now/table/{table}", json=data, params=params)["result"]

    def update(self, table, sys_id, data, params=None):
        if self.dry_run:
            print(f"[dry-run] UPDATE {table}/{sys_id}: {data}")
            return {"sys_id": sys_id, **data}
        params = {"sysparm_exclude_reference_link": "true", **(params or {})}
        return self._request("PATCH", f"/api/now/table/{table}/{sys_id}", json=data, params=params)["result"]

    def delete(self, table, sys_id):
        if self.dry_run:
            print(f"[dry-run] DELETE {table}/{sys_id}")
            return
        self._request("DELETE", f"/api/now/table/{table}/{sys_id}")


def connect_or_exit(dry_run=False):
    """Build a client and confirm the instance answers; print a clear message and exit 2 if not."""
    try:
        client = SNClient(dry_run=dry_run)
        client.get("sys_user", fields=["sys_id"], limit=1)
        return client
    except SNError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
