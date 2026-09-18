"""Thin, retrying REST client for PingIDM / ForgeRock Identity Management.

Authentication follows the IDM REST convention of passing the service
account in request headers::

    X-OpenIDM-Username: <username>
    X-OpenIDM-Password: <password>

The client handles:
- paged queries via ``_pagedResultsOffset`` / ``_pageSize``
- retries with exponential backoff on 429 and 5xx responses
- client-side rate limiting so bulk scans don't hammer a production IDM
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterator, List, Optional

import requests

from .config import ToolkitConfig

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class IdmError(Exception):
    """Raised when an IDM REST call fails after retries."""


class IdmClient:
    def __init__(self, config: ToolkitConfig) -> None:
        config.validate()
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(
            {
                "X-OpenIDM-Username": config.username,
                "X-OpenIDM-Password": config.password,
                "Accept-API-Version": "resource=1.0",
                "Content-Type": "application/json",
            }
        )
        self._last_call = 0.0

    # -- internals -----------------------------------------------------
    def _throttle(self) -> None:
        """Enforce the configured max requests-per-second."""
        min_interval = 1.0 / max(self.config.rate_limit_per_second, 0.1)
        elapsed = time.monotonic() - self._last_call
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_call = time.monotonic()

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.config.base_url}/{path.lstrip('/')}"
        kwargs.setdefault("timeout", self.config.timeout)
        kwargs.setdefault("verify", self.config.verify_ssl)

        last_exc: Optional[Exception] = None
        for attempt in range(self.config.max_retries + 1):
            self._throttle()
            try:
                resp = self.session.request(method, url, **kwargs)
            except requests.RequestException as exc:  # network-level failure
                last_exc = exc
                log.warning("Request failed (attempt %d): %s", attempt + 1, exc)
            else:
                if resp.status_code not in RETRYABLE_STATUS:
                    if resp.status_code >= 400:
                        raise IdmError(
                            f"{method} {path} -> HTTP {resp.status_code}: "
                            f"{resp.text[:500]}"
                        )
                    return resp
                log.warning(
                    "Retryable HTTP %d on %s %s (attempt %d)",
                    resp.status_code,
                    method,
                    path,
                    attempt + 1,
                )
                last_exc = IdmError(f"HTTP {resp.status_code}: {resp.text[:200]}")
            backoff = self.config.backoff_factor * (2**attempt)
            time.sleep(backoff)
        raise IdmError(f"{method} {path} failed after retries: {last_exc}")

    # -- reads ---------------------------------------------------------
    def query(
        self,
        resource: str,
        query_filter: str = "true",
        fields: Optional[List[str]] = None,
        page_size: Optional[int] = None,
        sort_keys: Optional[str] = None,
    ) -> Iterator[Dict[str, Any]]:
        """Yield every object matching a query, transparently paging.

        Args:
            resource: e.g. ``managed/user``, ``repo/link``, ``audit/recon``.
            query_filter: IDM query filter string, e.g. ``userName eq "jdoe"``.
            fields: limit returned fields via ``_fields``.
        """
        size = page_size or self.config.page_size
        offset = 0
        while True:
            params: Dict[str, Any] = {
                "_queryFilter": query_filter,
                "_pageSize": size,
                "_pagedResultsOffset": offset,
            }
            if fields:
                params["_fields"] = ",".join(fields)
            if sort_keys:
                params["_sortKeys"] = sort_keys
            resp = self._request("GET", f"openidm/{resource}", params=params)
            data = resp.json()
            results = data.get("result", [])
            if not results:
                break
            yield from results
            if len(results) < size:
                break
            offset += size

    def query_all(self, *args: Any, **kwargs: Any) -> List[Dict[str, Any]]:
        """Materialise :meth:`query` into a list (careful with large sets)."""
        return list(self.query(*args, **kwargs))

    def read(
        self, resource: str, object_id: str, fields: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        params = {"_fields": ",".join(fields)} if fields else None
        resp = self._request("GET", f"openidm/{resource}/{object_id}", params=params)
        return resp.json()

    def exists(self, resource: str, object_id: str) -> bool:
        try:
            self._request("HEAD", f"openidm/{resource}/{object_id}")
            return True
        except IdmError as exc:
            if "HTTP 404" in str(exc):
                return False
            raise

    # -- writes (used by bulk_remediator; always journal these) --------
    def patch(
        self, resource: str, object_id: str, operations: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Apply JSON-patch style operations, e.g.
        [{"operation": "replace", "field": "/mail", "value": "a@b.c"}]."""
        resp = self._request(
            "PATCH", f"openidm/{resource}/{object_id}", json=operations
        )
        return resp.json()

    def update(
        self, resource: str, object_id: str, obj: Dict[str, Any]
    ) -> Dict[str, Any]:
        resp = self._request("PUT", f"openidm/{resource}/{object_id}", json=obj)
        return resp.json()

    def create(self, resource: str, obj: Dict[str, Any]) -> Dict[str, Any]:
        resp = self._request("POST", f"openidm/{resource}?_action=create", json=obj)
        return resp.json()

    def delete(self, resource: str, object_id: str) -> None:
        self._request("DELETE", f"openidm/{resource}/{object_id}")
