from __future__ import annotations

import datetime as dt

from pbi_watchdog.config import CapacityConfig, MetricsSourceConfig
from pbi_watchdog.rest import ApiError
from pbi_watchdog.sources import MetricsAppRestSource, MetricsAppSempySource
from pbi_watchdog.sources import profiles as prof


class ScriptedClient:
    def __init__(self):
        self.calls = []

    def execute_dax(self, workspace_id, dataset_id, dax, *, timeout=120):
        self.calls.append(dax)
        if "SUM(MetricsByItemandOperationandDay[sum_CU])" in dax:
            raise ApiError("POST", "executeQueries", 400, "coluna sum_CU não encontrada")
        return [
            {
                "Items[ItemId]": "item-1",
                "Items[ItemName]": "Vendas",
                "Items[ItemKind]": "SemanticModel",
                "Items[WorkspaceId]": "ws-1",
                "Items[WorkspaceName]": "Financeiro",
                "[cu_seconds_today]": 1234.0,
            }
        ]


def config(**patch):
    raw = {"kind": "metrics_app_rest", "dataset_id": "ds-1", **patch}
    return MetricsSourceConfig.model_validate(raw)


def capacity():
    return CapacityConfig(key="F64", id="cap-64", sku="F64")


def test_auto_testa_dax_real_e_cai_do_v1_para_v2():
    client = ScriptedClient()
    source = MetricsAppRestSource(config(), client)

    rows = source.snapshot(capacity(), now=dt.datetime(2026, 7, 24, 12, 0))

    assert source.resolve_profile("cap-64").name == "fabric_metrics_v2"
    assert rows[0].item_id == "item-1"
    assert len(client.calls) == 2
    assert all("INFO." not in dax for dax in client.calls)


def test_dax_override_aceita_aliases_canonicos_do_contrato():
    client = ScriptedClient()
    client.execute_dax = lambda *args, **kwargs: [
        {
            "[item_id]": "item-custom",
            "[item_name]": "Modelo customizado",
            "[item_kind]": "SemanticModel",
            "[workspace_id]": "ws",
            "[workspace_name]": "Workspace",
            "[cu_seconds_today]": 42,
        }
    ]
    source = MetricsAppRestSource(config(dax_override="EVALUATE ROW(...)"), client)

    rows = source.snapshot(capacity(), now=dt.datetime(2026, 7, 24, 12, 0))

    assert len(rows) == 1
    assert rows[0].item_id == "item-custom"
    assert rows[0].cu_seconds_today == 42


def test_sempy_respeita_dax_override_sem_tentar_introspeccao():
    cfg = MetricsSourceConfig.model_validate(
        {
            "kind": "metrics_app_sempy",
            "workspace_name": "WS",
            "dataset_name": "Metrics",
            "dax_override": "EVALUATE ROW(...)",
        }
    )
    source = MetricsAppSempySource(cfg)

    assert source.resolve_profile().name == "custom"


def test_normalize_row_preserva_colunas_fisicas_dos_perfis():
    row = prof.normalize_row(
        {"Items[ItemId]": "x", "[cu_seconds_today]": 10},
        prof.FABRIC_METRICS_V1,
    )

    assert row == {"item_id": "x", "cu_seconds_today": 10}
