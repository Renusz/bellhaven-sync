"""CRM API client: reads with pagination and retries, writes that report exactly what happened."""
from __future__ import annotations

import logging
from typing import Any, Callable

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config

log = logging.getLogger(__name__)

PAGE_SIZE = 100


class CRMError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, detail: Any):
        super().__init__(f"{method} {path} -> {status}: {detail}")
        self.status, self.detail = status, detail


class CRMClient:
    """Thin wrapper over the sandbox API. `on_write` is called after every write (used for the audit log)."""

    def __init__(self, base_url: str | None = None, token: str | None = None,
                 on_write: Callable[[dict], None] | None = None):
        self.base = (base_url or config.CRM_BASE_URL).rstrip("/")
        token = token or config.CRM_TOKEN
        if not token:
            raise RuntimeError("CRM_TOKEN is not set. Copy .env.example to .env and add your token.")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {token}", "User-Agent": config.USER_AGENT})
        # GET and PATCH are safe to retry. POST is not: a retried create could make a second account.
        retry = Retry(total=3, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET", "PATCH"])
        self.s.mount("https://", HTTPAdapter(max_retries=retry))
        self.s.mount("http://", HTTPAdapter(max_retries=retry))
        self.on_write = on_write

    # ------------------------------------------------------------------ low level
    def _request(self, method: str, path: str, **kw) -> Any:
        r = self.s.request(method, self.base + path, timeout=30, **kw)
        try:
            body = r.json()
        except ValueError:
            body = r.text
        if method != "GET" and self.on_write:
            self.on_write({"method": method, "path": path, "request": kw.get("json"), "status": r.status_code, "response": body})
        if r.status_code >= 400:
            detail = body.get("detail") if isinstance(body, dict) else body
            raise CRMError(method, path, r.status_code, detail)
        return body

    def _paginate(self, path: str, params: dict | None = None) -> list[dict]:
        rows, page = [], 1
        while True:
            body = self._request("GET", path, params={**(params or {}), "page": page, "page_size": PAGE_SIZE})
            data = body.get("data", [])
            rows.extend(data)
            total = body.get("total", len(rows))
            if not data or len(rows) >= total:
                return rows
            page += 1

    # ------------------------------------------------------------------ reads
    def me(self) -> dict:
        return self._request("GET", "/me")

    def list_accounts(self, **filters) -> list[dict]:
        return self._paginate("/accounts", filters)

    def list_contacts(self, **filters) -> list[dict]:
        return self._paginate("/contacts", filters)

    def get_account(self, account_id: str) -> dict:
        return self._request("GET", f"/accounts/{account_id}")

    def find_account_by_name(self, name: str) -> dict | None:
        exact = [a for a in self.list_accounts(q=name) if a["name"] == name]
        return exact[0] if len(exact) == 1 else None

    # ------------------------------------------------------------------ writes
    def update_account(self, account_id: str, fields: dict) -> dict:
        return self._request("PATCH", f"/accounts/{account_id}", json=fields)

    def create_account(self, fields: dict) -> dict:
        return self._request("POST", "/accounts", json=fields)
