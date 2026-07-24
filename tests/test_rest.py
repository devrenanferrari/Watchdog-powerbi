"""Rotas do executeQueries. A escolha entre /groups/{ws}/datasets e /datasets decide se a
leitura do Metrics App funciona quando ele veio do AppSource."""

from __future__ import annotations

import pytest

from pbi_watchdog.rest import PBI_BASE, ApiError, RestClient


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload or {}
        self.text = ""
        self.headers = {}

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, payload=None, status=200):
        self.calls = []
        # `payload or {...}` trataria {} como ausente e mascararia o teste de resposta vazia.
        self.payload = (
            {"results": [{"tables": [{"rows": [{"[ping]": 1}]}]}]} if payload is None else payload
        )
        self.status = status

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw.get("json")))
        return FakeResponse(self.status, self.payload)


class FakeTokens:
    def get_token(self, scope):
        return "token-falso"


def client(session):
    return RestClient(FakeTokens(), session=session)


def test_com_workspace_usa_a_rota_de_grupo():
    s = FakeSession()
    client(s).execute_dax("ws-1", "ds-1", "EVALUATE ROW(\"ping\",1)")
    assert s.calls[0][1] == f"{PBI_BASE}/groups/ws-1/datasets/ds-1/executeQueries"


@pytest.mark.parametrize("vazio", [None, ""])
def test_sem_workspace_usa_a_rota_direta(vazio):
    """Conteúdo de app instalado vive no Meu workspace: não há groupId para citar."""
    s = FakeSession()
    client(s).execute_dax(vazio, "ds-1", "EVALUATE ROW(\"ping\",1)")
    assert s.calls[0][1] == f"{PBI_BASE}/datasets/ds-1/executeQueries"
    assert "/groups/" not in s.calls[0][1]


def test_devolve_as_linhas_da_primeira_tabela():
    s = FakeSession(payload={"results": [{"tables": [{"rows": [{"a": 1}, {"a": 2}]}]}]})
    assert client(s).execute_dax(None, "ds", "EVALUATE X") == [{"a": 1}, {"a": 2}]


@pytest.mark.parametrize(
    "payload", [{}, {"results": []}, {"results": [{"tables": []}]}, {"results": [{"tables": [{}]}]}]
)
def test_resposta_vazia_devolve_lista_vazia(payload):
    s = FakeSession(payload=payload)
    assert client(s).execute_dax(None, "ds", "EVALUATE X") == []


def test_erro_http_vira_ApiError_com_contexto():
    s = FakeSession(status=403)
    with pytest.raises(ApiError) as e:
        client(s).execute_dax("ws", "ds", "EVALUATE X")
    assert e.value.status == 403
    assert "executeQueries" in str(e.value)


def test_allowed_status_nao_levanta():
    s = FakeSession(status=404)
    resp = client(s).request("GET", f"{PBI_BASE}/qualquer", allowed_status=(404,))
    assert resp.status_code == 404
