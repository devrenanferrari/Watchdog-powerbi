"""Descoberta do Metrics App — o primeiro obstáculo real de quem instala a lib."""

from __future__ import annotations

import pytest

from pbi_watchdog.discover import (
    MetricsCandidate,
    find_metrics_app_rest,
    render,
    suggest_config,
)


class FakeClient:
    """Simula as duas chamadas REST usadas pela descoberta."""

    def __init__(self, groups, datasets_by_group):
        self.groups = groups
        self.datasets_by_group = datasets_by_group

    def get(self, url, **kw):
        if url.endswith("/groups"):
            return {"value": self.groups}
        gid = url.split("/groups/")[1].split("/")[0]
        return {"value": self.datasets_by_group.get(gid, [])}


def test_encontra_o_metrics_app_pelo_nome_padrao():
    client = FakeClient(
        groups=[
            {"id": "g1", "name": "Vendas"},
            {"id": "g2", "name": "Microsoft Fabric Capacity Metrics"},
        ],
        datasets_by_group={"g2": [{"id": "d2", "name": "Fabric Capacity Metrics"}]},
    )
    found = find_metrics_app_rest(client)
    assert len(found) == 1
    assert found[0].workspace_id == "g2"
    assert found[0].dataset_id == "d2"


def test_ordena_o_candidato_mais_provavel_primeiro():
    client = FakeClient(
        groups=[
            {"id": "g1", "name": "Metricas do time"},
            {"id": "g2", "name": "Microsoft Fabric Capacity Metrics"},
        ],
        datasets_by_group={
            "g1": [{"id": "d1", "name": "Metricas ad hoc"}],
            "g2": [{"id": "d2", "name": "Fabric Capacity Metrics"}],
        },
    )
    found = find_metrics_app_rest(client)
    assert found[0].workspace_id == "g2"  # nome oficial vence


def test_workspace_renomeado_so_aparece_com_include_all():
    """O caso que quebra na prática: alguém renomeou o workspace para algo irreconhecível."""
    client = FakeClient(
        groups=[{"id": "g9", "name": "Governanca BI"}],
        datasets_by_group={"g9": [{"id": "d9", "name": "Fabric Capacity Metrics"}]},
    )
    assert find_metrics_app_rest(client) == []
    achados = find_metrics_app_rest(client, include_all=True)
    assert len(achados) == 1
    assert achados[0].dataset_name == "Fabric Capacity Metrics"


def test_workspace_sem_permissao_nao_interrompe_a_busca():
    from pbi_watchdog.rest import ApiError

    class Flaky(FakeClient):
        def get(self, url, **kw):
            if "/groups/g1/" in url:
                raise ApiError("GET", url, 403, "forbidden")
            return super().get(url, **kw)

    client = Flaky(
        groups=[
            {"id": "g1", "name": "Capacity privado"},
            {"id": "g2", "name": "Fabric Capacity Metrics"},
        ],
        datasets_by_group={"g2": [{"id": "d2", "name": "Fabric Capacity Metrics"}]},
    )
    found = find_metrics_app_rest(client)
    assert [c.workspace_id for c in found] == ["g2"]


def test_gera_bloco_de_config_rest():
    c = MetricsCandidate("ws-1", "WS", "ds-1", "DS")
    assert c.as_config() == {
        "kind": "metrics_app_rest",
        "workspace_id": "ws-1",
        "dataset_id": "ds-1",
        "profile": "auto",
    }


def test_gera_bloco_de_config_sempy_com_nomes():
    """Dentro do Fabric a config usa nomes, não GUIDs."""
    c = MetricsCandidate("ws-1", "Meu Workspace", "ds-1", "Meu Dataset")
    cfg = c.as_config(kind="metrics_app_sempy")
    assert cfg["workspace_name"] == "Meu Workspace"
    assert cfg["dataset_name"] == "Meu Dataset"
    assert "workspace_id" not in cfg


def test_render_sem_candidatos_orienta_o_proximo_passo():
    saida = render([])
    assert "AppSource" in saida
    assert "include_all" in saida


def test_render_mostra_o_bloco_pronto_para_colar():
    saida = render([MetricsCandidate("ws-1", "WS", "ds-1", "DS", score=9)])
    assert "metrics_source" in saida
    assert "ws-1" in saida


def test_suggest_config_vazio_devolve_none():
    assert suggest_config([], kind="metrics_app_rest") is None


@pytest.mark.parametrize("nome", ["Microsoft Fabric Capacity Metrics", "Fabric Capacity Metrics"])
def test_nomes_conhecidos_pontuam_alto(nome):
    from pbi_watchdog.discover import _score

    assert _score(nome, "Fabric Capacity Metrics") >= 9
