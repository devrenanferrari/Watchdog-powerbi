"""Cliente HTTP fino para as APIs Power BI e Fabric: retry, throttling e erros legíveis."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

import requests

from .auth import SCOPE_FABRIC, SCOPE_POWERBI, TokenProvider

log = logging.getLogger(__name__)

PBI_BASE = "https://api.powerbi.com/v1.0/myorg"
FABRIC_BASE = "https://api.fabric.microsoft.com/v1"

_RETRYABLE = {429, 500, 502, 503, 504}


class ApiError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: str) -> None:
        self.status = status
        self.body = body
        super().__init__(f"{method} {url} -> HTTP {status}: {body[:500]}")


class RestClient:
    def __init__(
        self,
        tokens: TokenProvider,
        *,
        timeout: int = 60,
        max_retries: int = 4,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.tokens = tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()

    def _scope_for(self, url: str) -> str:
        return SCOPE_FABRIC if url.startswith(FABRIC_BASE) else SCOPE_POWERBI

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        allowed_status: tuple = (),
    ) -> requests.Response:
        """Executa com retry exponencial em 429/5xx, respeitando Retry-After.

        `allowed_status` marca códigos que o chamador trata como resposta válida
        (ex.: 404 ao consultar jobs de um item que não suporta jobs).
        """
        last: Optional[requests.Response] = None
        for attempt in range(self.max_retries + 1):
            token = self.tokens.get_token(self._scope_for(url))
            resp = self.session.request(
                method,
                url,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json=json,
                params=params,
                timeout=timeout or self.timeout,
            )
            last = resp
            if resp.status_code < 400 or resp.status_code in allowed_status:
                return resp
            if resp.status_code not in _RETRYABLE or attempt == self.max_retries:
                raise ApiError(method, url, resp.status_code, resp.text)

            wait = float(resp.headers.get("Retry-After", 2 ** attempt))
            log.warning("HTTP %s em %s; retry em %.0fs", resp.status_code, url, wait)
            time.sleep(min(wait, 60))

        raise ApiError(method, url, last.status_code if last else 0, last.text if last else "")

    def get(self, url: str, **kw) -> Dict[str, Any]:
        return self.request("GET", url, **kw).json()

    def post(self, url: str, **kw) -> requests.Response:
        return self.request("POST", url, **kw)

    def delete(self, url: str, **kw) -> requests.Response:
        return self.request("DELETE", url, **kw)

    # ------------------------------------------------------------------ DAX

    def execute_dax(
        self, workspace_id: str, dataset_id: str, dax: str, *, timeout: int = 120
    ) -> list:
        """Executa DAX via `executeQueries`. É o que torna a leitura do Metrics App portátil —
        sem XMLA, sem ADOMD, sem Windows.

        Requer no tenant: "Dataset Execute Queries REST API" habilitado, e o principal
        com permissão de leitura no workspace do Metrics App.
        """
        url = f"{PBI_BASE}/groups/{workspace_id}/datasets/{dataset_id}/executeQueries"
        payload = {
            "queries": [{"query": dax}],
            "serializerSettings": {"includeNulls": True},
        }
        resp = self.post(url, json=payload, timeout=timeout)
        data = resp.json()
        results = data.get("results") or []
        if not results:
            return []
        tables = results[0].get("tables") or []
        if not tables:
            return []
        return tables[0].get("rows") or []
