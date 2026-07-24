"""Aquisição de token. Um provider por forma de autenticação; o resto da lib só vê `get_token(scope)`."""

from __future__ import annotations

import time
from typing import ClassVar, Dict, Optional, Protocol

from ..config import AuthConfig, ManagedIdentityAuth, NotebookAuth, ServicePrincipalAuth

#: Recursos que a lib consome. O Fabric aceita tokens do recurso Power BI hoje,
#: mas mantemos separados porque isso já mudou antes.
SCOPE_POWERBI = "https://analysis.windows.net/powerbi/api/.default"
SCOPE_FABRIC = "https://api.fabric.microsoft.com/.default"


class TokenProvider(Protocol):
    def get_token(self, scope: str) -> str: ...


class _CachedProvider:
    """Cache em memória com margem de 5 min antes do expiry."""

    def __init__(self) -> None:
        self._cache: Dict[str, tuple] = {}

    def _cached(self, scope: str) -> Optional[str]:
        entry = self._cache.get(scope)
        if entry and entry[1] > time.time() + 300:
            return entry[0]
        return None

    def _store(self, scope: str, token: str, expires_in: int) -> str:
        self._cache[scope] = (token, time.time() + expires_in)
        return token


class ServicePrincipalProvider(_CachedProvider):
    def __init__(self, cfg: ServicePrincipalAuth) -> None:
        super().__init__()
        self.cfg = cfg
        self._app = None

    def _client(self):
        if self._app is not None:
            return self._app
        try:
            import msal
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("msal não instalado. `pip install pbi-watchdog`") from e

        authority = f"https://login.microsoftonline.com/{self.cfg.tenant_id}"
        if self.cfg.client_secret:
            credential = self.cfg.client_secret
        else:
            with open(self.cfg.certificate_path, "rb") as fh:  # type: ignore[arg-type]
                credential = {
                    "private_key": fh.read(),
                    "thumbprint": self.cfg.certificate_thumbprint,
                }
        self._app = msal.ConfidentialClientApplication(
            self.cfg.client_id, authority=authority, client_credential=credential
        )
        return self._app

    def get_token(self, scope: str) -> str:
        hit = self._cached(scope)
        if hit:
            return hit
        result = self._client().acquire_token_for_client(scopes=[scope])
        if "access_token" not in result:
            raise RuntimeError(
                "Falha ao obter token do service principal: "
                f"{result.get('error')} — {result.get('error_description')}"
            )
        return self._store(scope, result["access_token"], int(result.get("expires_in", 3600)))


class ManagedIdentityProvider(_CachedProvider):
    def __init__(self, cfg: ManagedIdentityAuth) -> None:
        super().__init__()
        self.cfg = cfg
        self._cred = None

    def get_token(self, scope: str) -> str:
        hit = self._cached(scope)
        if hit:
            return hit
        try:
            from azure.identity import ManagedIdentityCredential
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Instale o extra: `pip install pbi-watchdog[azure]`") from e
        if self._cred is None:
            self._cred = ManagedIdentityCredential(client_id=self.cfg.client_id)
        tok = self._cred.get_token(scope)
        return self._store(scope, tok.token, int(tok.expires_on - time.time()))


class NotebookProvider(_CachedProvider):
    """Identidade do notebook Fabric. Só funciona dentro do runtime do Fabric."""

    #: O notebookutils pede o recurso, não o scope com sufixo /.default.
    _RESOURCE: ClassVar[Dict[str, str]] = {
        SCOPE_POWERBI: "https://analysis.windows.net/powerbi/api",
        SCOPE_FABRIC: "https://api.fabric.microsoft.com",
    }

    def get_token(self, scope: str) -> str:
        hit = self._cached(scope)
        if hit:
            return hit
        try:
            import notebookutils  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "auth.kind='notebook' só funciona dentro de um notebook Fabric. "
                "Fora dele, use kind='service_principal'."
            ) from e
        token = notebookutils.credentials.getToken(self._RESOURCE.get(scope, scope))
        return self._store(scope, token, 3000)


class StaticTokenProvider(_CachedProvider):
    """Para testes e para quem já tem um token pronto."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self.token = token

    def get_token(self, scope: str) -> str:
        return self.token


def build_token_provider(cfg: AuthConfig) -> TokenProvider:
    if isinstance(cfg, ServicePrincipalAuth):
        return ServicePrincipalProvider(cfg)
    if isinstance(cfg, ManagedIdentityAuth):
        return ManagedIdentityProvider(cfg)
    if isinstance(cfg, NotebookAuth):
        return NotebookProvider()
    raise ValueError(f"Tipo de auth não suportado: {cfg!r}")
