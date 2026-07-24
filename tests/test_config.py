"""A config é o contrato com o usuário: erro de digitação tem que falhar alto e explicado."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from pbi_watchdog.config import WatchdogConfig

MINIMAL = {
    "version": 1,
    "auth": {
        "kind": "service_principal",
        "tenant_id": "t",
        "client_id": "c",
        "client_secret": "s",
    },
    "metrics_source": {"kind": "metrics_app_rest", "workspace_id": "w", "dataset_id": "d"},
    "capacities": [{"key": "F64", "id": "guid-64"}],
}


def cfg(**patch) -> dict:
    import copy

    out = copy.deepcopy(MINIMAL)
    out.update(patch)
    return out


def test_config_minima_valida_e_aplica_defaults():
    c = WatchdogConfig.from_dict(cfg())
    cap = c.capacity("F64")
    assert cap.policy.mode == "observe"  # padrão seguro
    assert cap.policy.thresholds.alert == 1.2
    assert cap.policy.guards.consecutive_breaches == 2


def test_overrides_fazem_merge_por_bloco_sem_apagar_o_resto():
    c = WatchdogConfig.from_dict(
        cfg(capacities=[{"key": "F64", "id": "g", "overrides": {"thresholds": {"alert": 1.05}}}])
    )
    t = c.capacity("F64").policy.thresholds
    assert t.alert == 1.05
    assert t.throttle == 1.5  # preservado do default
    assert t.kill == 1.8


def test_capacidades_isolam_politicas():
    c = WatchdogConfig.from_dict(
        cfg(
            capacities=[
                {"key": "PROD", "id": "g1", "overrides": {"mode": "observe"}},
                {"key": "SANDBOX", "id": "g2", "overrides": {"mode": "enforce"}},
            ]
        )
    )
    assert c.capacity("PROD").policy.mode == "observe"
    assert c.capacity("SANDBOX").policy.mode == "enforce"


def test_chave_desconhecida_e_erro_nao_silencio():
    with pytest.raises(ValidationError):
        WatchdogConfig.from_dict(cfg(thresholdz={"alert": 2}))


def test_override_de_campo_inexistente_lista_os_validos():
    with pytest.raises(ValidationError) as e:
        WatchdogConfig.from_dict(
            cfg(capacities=[{"key": "F64", "id": "g", "overrides": {"treshold": {}}}])
        )
    assert "não é um campo de política válido" in str(e.value)


def test_thresholds_precisam_ser_crescentes():
    with pytest.raises(ValidationError) as e:
        WatchdogConfig.from_dict(cfg(defaults={"thresholds": {"alert": 2.0, "throttle": 1.5, "kill": 1.8}}))
    assert "crescentes" in str(e.value)


def test_service_principal_sem_credencial_falha_com_instrucao():
    with pytest.raises(ValidationError) as e:
        WatchdogConfig.from_dict(cfg(auth={"kind": "service_principal", "tenant_id": "t", "client_id": "c"}))
    assert "client_secret" in str(e.value)


def test_metrics_rest_sem_guids_aponta_o_comando_de_descoberta():
    with pytest.raises(ValidationError) as e:
        WatchdogConfig.from_dict(cfg(metrics_source={"kind": "metrics_app_rest"}))
    assert "discover" in str(e.value)


def test_capacidades_duplicadas_sao_rejeitadas():
    with pytest.raises(ValidationError) as e:
        WatchdogConfig.from_dict(cfg(capacities=[{"key": "X", "id": "1"}, {"key": "X", "id": "2"}]))
    assert "duplicadas" in str(e.value)


def test_lista_de_capacidades_vazia_e_rejeitada():
    with pytest.raises(ValidationError):
        WatchdogConfig.from_dict(cfg(capacities=[]))


def test_interpolacao_de_ambiente(monkeypatch):
    monkeypatch.setenv("MEU_SEGREDO", "abc123")
    c = WatchdogConfig.from_dict(
        cfg(auth={"kind": "service_principal", "tenant_id": "t", "client_id": "c",
                  "client_secret": "${MEU_SEGREDO}"})
    )
    assert c.auth.client_secret == "abc123"


def test_interpolacao_com_default(monkeypatch):
    monkeypatch.delenv("NAO_EXISTE", raising=False)
    c = WatchdogConfig.from_dict(cfg(timezone="${NAO_EXISTE:-America/Sao_Paulo}"))
    assert c.timezone == "America/Sao_Paulo"


def test_variavel_faltante_sem_default_falha_explicando(monkeypatch):
    monkeypatch.delenv("PBI_SEGREDO_AUSENTE", raising=False)
    with pytest.raises(ValueError) as e:
        WatchdogConfig.from_dict(
            cfg(auth={"kind": "service_principal", "tenant_id": "t", "client_id": "c",
                      "client_secret": "${PBI_SEGREDO_AUSENTE}"})
        )
    assert "PBI_SEGREDO_AUSENTE" in str(e.value)


def test_enabled_capacities_filtra_desligadas():
    c = WatchdogConfig.from_dict(
        cfg(capacities=[{"key": "A", "id": "1"}, {"key": "B", "id": "2", "enabled": False}])
    )
    assert [x.key for x in c.enabled_capacities] == ["A"]


def test_template_de_exemplo_e_valido(monkeypatch):
    """O arquivo que `pbi-watchdog init` entrega precisa passar na própria validação."""
    from pathlib import Path

    import pbi_watchdog

    monkeypatch.setenv("PBI_CLIENT_ID", "x")
    monkeypatch.setenv("PBI_CLIENT_SECRET", "y")
    monkeypatch.setenv("TEAMS_WEBHOOK_URL", "https://example.invalid/hook")
    template = Path(pbi_watchdog.__file__).parent / "templates" / "watchdog.example.yaml"
    c = WatchdogConfig.from_file(template)
    assert len(c.capacities) == 3
    assert all(cap.policy.mode == "observe" for cap in c.enabled_capacities)
