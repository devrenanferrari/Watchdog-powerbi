"""End-to-end com fonte sintética e SQLite temporário: nada de rede."""

from __future__ import annotations

import datetime as dt

import pytest

from pbi_watchdog.config import WatchdogConfig
from pbi_watchdog.models import ItemSnapshot
from pbi_watchdog.runner import Watchdog
from pbi_watchdog.storage.sqlite_store import SqliteStore


def make_config(tmp_path, **defaults) -> WatchdogConfig:
    base_defaults = {
        "mode": "enforce",
        "interval_minutes": 15,
        "baseline": {"min_days": 1, "method": "mean", "trim_top_percent": 0},
        "guards": {"min_cu_seconds": 100, "consecutive_breaches": 1, "cooldown_minutes": 0},
        "actions": {"alert": ["notify"], "throttle": ["notify"], "kill": ["notify"]},
        "notify": [{"kind": "none"}],
    }
    base_defaults.update(defaults)
    return WatchdogConfig.from_dict(
        {
            "version": 1,
            "auth": {"kind": "service_principal", "tenant_id": "t", "client_id": "c", "client_secret": "s"},
            "metrics_source": {"kind": "fake"},
            "storage": {"kind": "sqlite", "path": str(tmp_path / "wd.db")},
            "defaults": base_defaults,
            "capacities": [{"key": "F64", "id": "cap-64", "sku": "F64"}],
        }
    )


class ScriptedSource:
    """Fonte controlada: devolve o acumulado que o teste mandar, no instante que o teste mandar."""

    def __init__(self, script):
        self.script = script
        self.calls = 0

    def snapshot(self, capacity, *, now):
        cu = self.script[self.calls] if self.calls < len(self.script) else self.script[-1]
        self.calls += 1
        return [
            ItemSnapshot(
                ts=now, capacity_key=capacity.key, capacity_id=capacity.id, item_id="item-a",
                item_name="Vendas", item_kind="SemanticModel", workspace_id="ws-1",
                workspace_name="WS", cu_seconds_today=cu,
            )
        ]

    def describe(self):
        return "scripted"


def drive(wd, source, instantes):
    """Roda um ciclo por instante, injetando o relógio."""
    import pbi_watchdog.runner as runner_mod

    resultados = []
    for t in instantes:
        original = runner_mod._now
        runner_mod._now = lambda _cfg, _t=t: _t
        try:
            resultados.append(wd.run_once()[0])
        finally:
            runner_mod._now = original
    return resultados


@pytest.fixture
def clock():
    return [dt.datetime(2026, 7, 15 + d, 14, m) for d in range(8) for m in (0, 15)]


def test_primeiro_ciclo_e_bootstrap_sem_acao(tmp_path):
    wd = Watchdog(make_config(tmp_path))
    wd.source = ScriptedSource([1000.0])
    try:
        s = drive(wd, wd.source, [dt.datetime(2026, 7, 22, 14, 0)])[0]
        assert s.items_scanned == 1
        assert s.extra.get("bootstrap") is True
        assert s.anomalies == 0
    finally:
        wd.close()


def test_sem_historico_suficiente_nao_ha_baseline(tmp_path):
    wd = Watchdog(make_config(tmp_path, baseline={"min_days": 4}))
    wd.source = ScriptedSource([1000.0, 50000.0])
    try:
        resultados = drive(wd, wd.source, [
            dt.datetime(2026, 7, 22, 14, 0), dt.datetime(2026, 7, 22, 14, 15)
        ])
        assert resultados[1].anomalies == 0  # pico enorme, mas sem base de comparação
    finally:
        wd.close()


def test_pico_apos_historico_dispara_kill(tmp_path):
    """Sete dias de consumo estável às 14h, depois um pico de 10x."""
    config = make_config(tmp_path)
    wd = Watchdog(config)
    try:
        instantes, script = [], []
        for d in range(7):  # 15 a 21 de julho: 1000 CU·s por intervalo
            instantes += [dt.datetime(2026, 7, 15 + d, 14, 0), dt.datetime(2026, 7, 15 + d, 14, 15)]
            script += [1000.0, 2000.0]
        instantes += [dt.datetime(2026, 7, 22, 14, 0), dt.datetime(2026, 7, 22, 14, 15)]
        script += [1000.0, 11000.0]  # pico: 10000 CU·s no intervalo

        wd.source = ScriptedSource(script)
        resultados = drive(wd, wd.source, instantes)
        final = resultados[-1]

        assert final.anomalies == 1
        evento = final.events[0]
        assert evento.tier == "kill"
        assert evento.effective_tier == "kill"
        assert evento.ratio == pytest.approx(10.0, rel=0.01)
        assert evento.baseline_days == 7
    finally:
        wd.close()


def test_modo_observe_registra_mas_nao_escala(tmp_path):
    wd = Watchdog(make_config(tmp_path, mode="observe"))
    try:
        instantes, script = [], []
        for d in range(5):
            instantes += [dt.datetime(2026, 7, 15 + d, 14, 0), dt.datetime(2026, 7, 15 + d, 14, 15)]
            script += [1000.0, 2000.0]
        instantes += [dt.datetime(2026, 7, 22, 14, 0), dt.datetime(2026, 7, 22, 14, 15)]
        script += [1000.0, 11000.0]

        wd.source = ScriptedSource(script)
        final = drive(wd, wd.source, instantes)[-1]
        assert final.anomalies == 1
        assert final.events[0].tier == "kill"
        assert final.events[0].effective_tier == "alert"
        assert "observe_mode" in final.events[0].suppressions
        assert final.actions_taken == 0
    finally:
        wd.close()


def test_dry_run_nao_executa_acoes(tmp_path):
    config = make_config(tmp_path, actions={"alert": ["notify"], "throttle": ["notify"],
                                            "kill": ["notify", "cancel_refresh"]})
    wd = Watchdog(config, dry_run=True)
    chamadas = []

    class SpyAction:
        name = "cancel_refresh"

        def execute(self, a, *, dry_run):
            from pbi_watchdog.models import ActionResult

            chamadas.append(dry_run)
            return ActionResult(self.name, True, detail="[dry-run]")

    wd.actions["cancel_refresh"] = SpyAction()
    try:
        instantes, script = [], []
        for d in range(5):
            instantes += [dt.datetime(2026, 7, 15 + d, 14, 0), dt.datetime(2026, 7, 15 + d, 14, 15)]
            script += [1000.0, 2000.0]
        instantes += [dt.datetime(2026, 7, 22, 14, 0), dt.datetime(2026, 7, 22, 14, 15)]
        script += [1000.0, 11000.0]

        wd.source = ScriptedSource(script)
        drive(wd, wd.source, instantes)
        assert chamadas == [True]
    finally:
        wd.close()


def test_acao_que_falha_nao_derruba_o_ciclo(tmp_path):
    config = make_config(tmp_path, actions={"alert": ["notify"], "throttle": ["notify"],
                                            "kill": ["notify", "cancel_refresh"]})
    wd = Watchdog(config)

    class BrokenAction:
        name = "cancel_refresh"

        def execute(self, a, *, dry_run):
            raise RuntimeError("API fora do ar")

    wd.actions["cancel_refresh"] = BrokenAction()
    try:
        instantes, script = [], []
        for d in range(5):
            instantes += [dt.datetime(2026, 7, 15 + d, 14, 0), dt.datetime(2026, 7, 15 + d, 14, 15)]
            script += [1000.0, 2000.0]
        instantes += [dt.datetime(2026, 7, 22, 14, 0), dt.datetime(2026, 7, 22, 14, 15)]
        script += [1000.0, 11000.0]

        wd.source = ScriptedSource(script)
        final = drive(wd, wd.source, instantes)[-1]
        assert final.errors == []
        assert "cancel_refresh:fail" in final.events[0].actions
    finally:
        wd.close()


# --------------------------------------------------------------------- store


def test_sqlite_roundtrip_de_snapshots_e_estado(tmp_path):
    store = SqliteStore(str(tmp_path / "s.db"))
    try:
        t0 = dt.datetime(2026, 7, 22, 14, 0)
        rows = [
            ItemSnapshot(t0, "F64", "cap", "i1", "A", "SemanticModel", "ws", "WS", 100.0),
            ItemSnapshot(t0 + dt.timedelta(minutes=15), "F64", "cap", "i1", "A",
                         "SemanticModel", "ws", "WS", 250.0),
        ]
        store.write_snapshots(rows)

        anteriores = store.previous_snapshots("F64", before=t0 + dt.timedelta(minutes=30))
        assert anteriores["i1"].cu_seconds_today == 250.0

        anteriores = store.previous_snapshots("F64", before=t0 + dt.timedelta(minutes=10))
        assert anteriores["i1"].cu_seconds_today == 100.0
    finally:
        store.close()


def test_sqlite_filtra_intervalos_por_hora(tmp_path):
    from pbi_watchdog.models import Interval

    store = SqliteStore(str(tmp_path / "s.db"))
    try:
        store.write_intervals([
            Interval("F64", "i1", "A", "K", "ws", "WS",
                     dt.datetime(2026, 7, 20, 14, 0), dt.datetime(2026, 7, 20, 14, 15), 500.0),
            Interval("F64", "i1", "A", "K", "ws", "WS",
                     dt.datetime(2026, 7, 20, 3, 0), dt.datetime(2026, 7, 20, 3, 15), 900.0),
        ])
        assert len(store.load_intervals("F64", dt.date(2026, 7, 19), dt.date(2026, 7, 23), hours=[14])) == 1
        assert len(store.load_intervals("F64", dt.date(2026, 7, 19), dt.date(2026, 7, 23))) == 2
    finally:
        store.close()
