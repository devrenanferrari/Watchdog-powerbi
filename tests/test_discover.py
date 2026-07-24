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
    """Simula as chamadas REST usadas pela descoberta: apps, Meu workspace e workspaces."""

    def __init__(
        self,
        groups=None,
        datasets_by_group=None,
        apps=None,
        reports_by_app=None,
        my_datasets=None,
    ):
        self.groups = groups or []
        self.datasets_by_group = datasets_by_group or {}
        self.apps = apps or []
        self.reports_by_app = reports_by_app or {}
        self.my_datasets = my_datasets or []

    def get(self, url, **kw):
        if url.endswith("/apps"):
            return {"value": self.apps}
        if "/apps/" in url and url.endswith("/reports"):
            app_id = url.split("/apps/")[1].split("/")[0]
            return {"value": self.reports_by_app.get(app_id, [])}
        if url.endswith("/groups"):
            return {"value": self.groups}
        if url.endswith("/datasets") and "/groups/" not in url:
            return {"value": self.my_datasets}
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


def test_encontra_metrics_app_instalado_como_app():
    """O caso real: AppSource instala o conteúdo no Meu workspace, e nenhuma busca por
    workspaces o encontra. A URL do relatório mostra /groups/me/apps/<id>/."""
    client = FakeClient(
        apps=[{"id": "app-1", "name": "Microsoft Fabric Capacity Metrics F64001"}],
        reports_by_app={
            "app-1": [
                {"id": "rep-1", "name": "Fabric Capacity Metrics", "datasetId": "ds-metrics"},
                {"id": "rep-2", "name": "Outro relatório", "datasetId": "ds-metrics"},
            ]
        },
        groups=[],
    )
    found = find_metrics_app_rest(client)
    assert len(found) == 1, "relatórios do mesmo dataset devem ser deduplicados"
    assert found[0].dataset_id == "ds-metrics"
    assert found[0].origin == "app"
    assert found[0].in_my_workspace is True


def test_config_de_app_omite_workspace_id():
    """Sem groupId, o executeQueries usa a rota /datasets/{id} — passar workspace_id vazio
    montaria uma URL inválida."""
    client = FakeClient(
        apps=[{"id": "app-1", "name": "Fabric Capacity Metrics"}],
        reports_by_app={"app-1": [{"id": "r", "name": "Metrics", "datasetId": "ds-1"}]},
    )
    cfg = find_metrics_app_rest(client)[0].as_config()
    assert cfg == {"kind": "metrics_app_rest", "dataset_id": "ds-1", "profile": "auto"}
    assert "workspace_id" not in cfg


def test_encontra_no_meu_workspace():
    client = FakeClient(my_datasets=[{"id": "ds-9", "name": "Fabric Capacity Metrics"}])
    found = find_metrics_app_rest(client)
    assert found[0].dataset_id == "ds-9"
    assert found[0].origin == "my_workspace"


def test_dataset_alcancavel_pelos_dois_caminhos_aparece_uma_vez():
    """Quando o mesmo dataset é visível via app e via workspace, vence a forma com
    workspace_id explícito: a rota /groups/{ws}/datasets é mais específica, e o acesso
    direto ao workspace não depende do app continuar instalado."""
    client = FakeClient(
        apps=[{"id": "app-1", "name": "Fabric Capacity Metrics"}],
        reports_by_app={"app-1": [{"id": "r", "name": "Metrics", "datasetId": "ds-x"}]},
        groups=[{"id": "g1", "name": "Fabric Capacity Metrics"}],
        datasets_by_group={"g1": [{"id": "ds-x", "name": "Fabric Capacity Metrics"}]},
    )
    found = find_metrics_app_rest(client)
    assert len(found) == 1
    assert found[0].workspace_id == "g1"
    assert "workspace_id" in found[0].as_config()


def test_quando_so_o_app_enxerga_o_dataset_ele_e_escolhido():
    client = FakeClient(
        apps=[{"id": "app-1", "name": "Fabric Capacity Metrics"}],
        reports_by_app={"app-1": [{"id": "r", "name": "Metrics", "datasetId": "ds-x"}]},
        groups=[],
    )
    found = find_metrics_app_rest(client)
    assert found[0].origin == "app"
    assert found[0].in_my_workspace is True


def test_falha_ao_listar_apps_nao_impede_busca_por_workspaces():
    from pbi_watchdog.rest import ApiError

    class SemApps(FakeClient):
        def get(self, url, **kw):
            if url.endswith("/apps"):
                raise ApiError("GET", url, 403, "forbidden")
            return super().get(url, **kw)

    client = SemApps(
        groups=[{"id": "g1", "name": "Fabric Capacity Metrics"}],
        datasets_by_group={"g1": [{"id": "ds-1", "name": "Fabric Capacity Metrics"}]},
    )
    found = find_metrics_app_rest(client)
    assert [c.dataset_id for c in found] == ["ds-1"]


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
