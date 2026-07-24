"""O doctor é o que responde "o que falta para isto funcionar" — se ele mente, o usuário
descobre no primeiro incidente."""

from __future__ import annotations

import pytest

from pbi_watchdog import doctor as doc
from pbi_watchdog.config import WatchdogConfig


def make_config(tmp_path, **patch) -> WatchdogConfig:
    raw = {
        "version": 1,
        "auth": {"kind": "service_principal", "tenant_id": "t", "client_id": "c", "client_secret": "s"},
        "metrics_source": {"kind": "fake"},
        "storage": {"kind": "sqlite", "path": str(tmp_path / "d.db")},
        "defaults": {"mode": "observe", "notify": [{"kind": "console"}]},
        "capacities": [{"key": "F64", "id": "cap-64", "sku": "F64"}],
    }
    raw.update(patch)
    return WatchdogConfig.from_dict(raw)


def test_checagem_que_explode_usa_o_rotulo_de_negocio(tmp_path):
    """Sem isto, o usuário lê 'check_auth_powerbi' em vez de 'Token Power BI'."""
    d = doc.Doctor(make_config(tmp_path))
    checks = d.run()
    nomes = [c.name for c in checks]
    assert "Token Power BI" in nomes
    assert not any(n.startswith("check_") for n in nomes)


def test_falha_de_auth_vem_com_remedio(tmp_path):
    checks = doc.Doctor(make_config(tmp_path)).run()
    auth = next(c for c in checks if c.name == "Token Power BI")
    assert auth.status == doc.FAIL
    assert "tenant_id" in auth.remedy


def test_storage_gravavel_passa(tmp_path):
    checks = doc.Doctor(make_config(tmp_path)).run()
    assert next(c for c in checks if c.name == "Storage gravável").status == doc.OK


def test_fonte_fake_marca_checagens_de_metrica_como_nao_aplicaveis(tmp_path):
    checks = doc.Doctor(make_config(tmp_path)).run()
    assert next(c for c in checks if c.name == "Acesso ao Metrics App").status == doc.SKIP
    assert next(c for c in checks if c.name == "Perfil de DAX").status == doc.SKIP


def test_ausencia_de_canal_de_notificacao_e_aviso(tmp_path):
    config = make_config(tmp_path, defaults={"mode": "observe", "notify": []})
    check = next(c for c in doc.Doctor(config).run() if c.name == "Notificação")
    assert check.status == doc.WARN
    assert "sem avisar" in check.remedy


def test_enforce_sem_historico_e_bloqueado(tmp_path):
    """A checagem que impede o erro mais caro: armar o enforcement sem baseline."""
    config = make_config(
        tmp_path,
        defaults={"mode": "enforce", "notify": [{"kind": "console"}], "baseline": {"min_days": 4}},
    )
    check = next(c for c in doc.Doctor(config).run() if c.name == "Prontidão para enforce")
    assert check.status == doc.FAIL
    assert "observe" in check.remedy


def test_observe_e_considerado_pronto(tmp_path):
    check = next(
        c for c in doc.Doctor(make_config(tmp_path)).run() if c.name == "Prontidão para enforce"
    )
    assert check.status == doc.OK


def test_render_resume_e_aponta_a_documentacao(tmp_path):
    saida = doc.render(doc.Doctor(make_config(tmp_path)).run())
    assert "Resultado:" in saida
    assert "PERMISSIONS.md" in saida  # há falhas de auth nesta config


@pytest.mark.parametrize("status", [doc.OK, doc.WARN, doc.FAIL, doc.SKIP])
def test_todo_status_tem_icone(status):
    assert doc.Check("x", status).icon
