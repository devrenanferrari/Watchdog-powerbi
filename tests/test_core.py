"""Testes do núcleo de decisão. Sem rede, sem tenant, sem Spark."""

from __future__ import annotations

import datetime as dt

import pytest

from pbi_watchdog.config import BaselineConfig, PolicyConfig
from pbi_watchdog.core import baseline as bl
from pbi_watchdog.core import detect
from pbi_watchdog.models import (
    SUPPRESS_BELOW_MIN_CU,
    SUPPRESS_COOLDOWN,
    SUPPRESS_INSUFFICIENT_BASELINE,
    SUPPRESS_OBSERVE_MODE,
    SUPPRESS_PROTECTED,
    SUPPRESS_RUN_BUDGET,
    SUPPRESS_STREAK,
    Baseline,
    Interval,
    ItemSnapshot,
    ItemState,
    Tier,
)

T0 = dt.datetime(2026, 7, 22, 14, 0)


def snap(item_id="item-a", cu=1000.0, ts=T0, name="Vendas"):
    return ItemSnapshot(
        ts=ts, capacity_key="F64", capacity_id="cap-1", item_id=item_id, item_name=name,
        item_kind="SemanticModel", workspace_id="ws-1", workspace_name="WS", cu_seconds_today=cu,
    )


def interval(cu=1000.0, start=T0, minutes=15, item_id="item-a", kind="SemanticModel", name="Vendas"):
    return Interval(
        capacity_key="F64", item_id=item_id, item_name=name, item_kind=kind,
        workspace_id="ws-1", workspace_name="WS",
        window_start=start, window_end=start + dt.timedelta(minutes=minutes), cu_seconds=cu,
    )


def policy(**kw) -> PolicyConfig:
    base = {
        "mode": "enforce",
        "interval_minutes": 15,
        "guards": {"min_cu_seconds": 100, "consecutive_breaches": 1, "cooldown_minutes": 0},
        "baseline": {"min_days": 1},
    }
    base.update(kw)
    return PolicyConfig.model_validate(base)


# --------------------------------------------------------------------- intervalos


def test_derive_intervals_usa_diff_do_acumulado():
    prev = {"item-a": snap(cu=1000.0, ts=T0)}
    cur = [snap(cu=1750.0, ts=T0 + dt.timedelta(minutes=15))]
    out = bl.derive_intervals(cur, prev)
    assert len(out) == 1
    assert out[0].cu_seconds == 750.0
    assert out[0].minutes == 15


def test_derive_intervals_ignora_item_sem_snapshot_anterior():
    assert bl.derive_intervals([snap()], {}) == []


def test_derive_intervals_trata_virada_de_dia_como_consumo_do_dia():
    prev = {"item-a": snap(cu=90000.0, ts=dt.datetime(2026, 7, 21, 23, 50))}
    cur = [snap(cu=300.0, ts=dt.datetime(2026, 7, 22, 0, 5))]
    out = bl.derive_intervals(cur, prev)
    assert out[0].cu_seconds == 300.0  # não vira negativo


def test_derive_intervals_trata_reset_do_contador_no_mesmo_dia():
    prev = {"item-a": snap(cu=5000.0, ts=T0)}
    cur = [snap(cu=200.0, ts=T0 + dt.timedelta(minutes=15))]
    assert bl.derive_intervals(cur, prev)[0].cu_seconds == 200.0


# --------------------------------------------------------------------- normalização


def test_normalizacao_corrige_atraso_do_agendador():
    """Um intervalo de 45 min com 3x o consumo NÃO é uma anomalia — é o agendador atrasado."""
    atrasado = interval(cu=3000.0, minutes=45)
    assert bl.normalize_per_minute(atrasado, 15) == 1000.0


@pytest.mark.parametrize(
    "minutes,esperado",
    [(15, True), (40, True), (5, True), (4, False), (46, False), (1425, False)],
)
def test_is_comparable_rejeita_duracoes_destoantes(minutes, esperado):
    assert bl.is_comparable(interval(minutes=minutes), 15, 3.0) is esperado


def test_baseline_descarta_intervalo_de_gap():
    """Um gap de 24h traria uma amostra normalizada de ~10 CU·s que rebaixaria a baseline."""
    hist = [
        interval(cu=1000.0, minutes=15, start=dt.datetime(2026, 7, 20, 14, 0)),
        interval(cu=1000.0, minutes=1425, start=dt.datetime(2026, 7, 20, 14, 15)),
    ]
    cfg = BaselineConfig(min_days=1, method="mean", trim_top_percent=0)
    com_filtro = bl.compute_baselines(
        hist, cfg, target_bucket="h14", reference_date=dt.date(2026, 7, 22),
        target_minutes=15, max_stretch=3.0,
    )
    assert com_filtro["item-a"].samples == 1
    assert com_filtro["item-a"].value == 1000.0

    sem_filtro = bl.compute_baselines(
        hist, cfg, target_bucket="h14", reference_date=dt.date(2026, 7, 22), target_minutes=15
    )
    assert sem_filtro["item-a"].value < 600.0  # baseline envenenada


def test_baseline_normaliza_historico_de_cadencia_irregular():
    hist = [
        interval(cu=2000.0, minutes=30, start=dt.datetime(2026, 7, 20, 14, 0)),
        interval(cu=1000.0, minutes=15, start=dt.datetime(2026, 7, 21, 14, 0)),
    ]
    out = bl.compute_baselines(
        hist, BaselineConfig(min_days=1, method="mean", trim_top_percent=0),
        target_bucket="h14", reference_date=dt.date(2026, 7, 22), target_minutes=15,
    )
    assert out["item-a"].value == 1000.0  # ambos viram 1000 CU·s / 15 min
    assert out["item-a"].days == 2


# --------------------------------------------------------------------- baseline


def test_baseline_respeita_bucket_horario():
    hist = [
        interval(cu=1000.0, start=dt.datetime(2026, 7, 20, 14, 0)),
        interval(cu=9999.0, start=dt.datetime(2026, 7, 20, 3, 0)),  # outro bucket
    ]
    out = bl.compute_baselines(
        hist, BaselineConfig(min_days=1, method="mean", trim_top_percent=0),
        target_bucket="h14", reference_date=dt.date(2026, 7, 22), target_minutes=15,
    )
    assert out["item-a"].samples == 1


def test_baseline_exclui_o_dia_corrente():
    hist = [interval(cu=1000.0, start=dt.datetime(2026, 7, 22, 14, 0))]
    out = bl.compute_baselines(
        hist, BaselineConfig(), target_bucket="h14",
        reference_date=dt.date(2026, 7, 22), target_minutes=15,
    )
    assert out == {}


def test_baseline_trim_descarta_topo():
    hist = [
        interval(cu=cu, start=dt.datetime(2026, 7, 15 + i, 14, 0))
        for i, cu in enumerate([100.0, 100.0, 100.0, 10000.0])
    ]
    cfg = BaselineConfig(lookback_days=10, min_days=1, method="mean", trim_top_percent=25)
    out = bl.compute_baselines(
        hist, cfg, target_bucket="h14", reference_date=dt.date(2026, 7, 22), target_minutes=15
    )
    assert out["item-a"].value == 100.0  # o incidente de 10000 foi aparado


def test_bucket_hour_of_week_separa_dias():
    assert bl.bucket_key(dt.datetime(2026, 7, 22, 14, 0), "hour_of_week") == "d2h14"
    assert bl.bucket_key(dt.datetime(2026, 7, 22, 14, 0), "hour_of_day") == "h14"


# --------------------------------------------------------------------- classificação


@pytest.mark.parametrize(
    "ratio,expected",
    [(1.0, Tier.NONE), (1.19, Tier.NONE), (1.2, Tier.ALERT), (1.5, Tier.THROTTLE),
     (1.79, Tier.THROTTLE), (1.8, Tier.KILL), (None, Tier.NONE)],
)
def test_classify(ratio, expected):
    assert detect.classify(ratio, policy()) == expected


# --------------------------------------------------------------------- travas


def test_min_cu_bloqueia_item_ocioso():
    """3 CU·s contra baseline de 1 CU·s é 3x, e completamente irrelevante."""
    a = detect.assess_one(interval(cu=3.0), Baseline(1.0, 7, 7, "median"), policy(), None, now=T0)
    assert a.tier == Tier.KILL
    assert a.effective_tier == Tier.NONE
    assert SUPPRESS_BELOW_MIN_CU in a.suppressions


def test_baseline_insuficiente_derruba_para_none():
    p = policy(baseline={"min_days": 4})
    a = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 2, 2, "median"), p, None, now=T0)
    assert a.tier == Tier.KILL
    assert a.effective_tier == Tier.NONE
    assert SUPPRESS_INSUFFICIENT_BASELINE in a.suppressions


def test_observe_nunca_passa_de_alert():
    p = policy(mode="observe")
    a = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 7, 7, "median"), p, None, now=T0)
    assert a.tier == Tier.KILL
    assert a.effective_tier == Tier.ALERT
    assert SUPPRESS_OBSERVE_MODE in a.suppressions


def test_item_protegido_alerta_mas_nao_sofre_acao():
    p = policy(protect={"name_patterns": ["(?i)antt"]})
    a = detect.assess_one(
        interval(cu=5000.0, name="Relatório ANTT"), Baseline(1000.0, 7, 7, "median"), p, None, now=T0
    )
    assert a.tier == Tier.KILL
    assert a.effective_tier == Tier.ALERT
    assert SUPPRESS_PROTECTED in a.suppressions


def test_protecao_por_workspace_e_kind():
    p = policy(protect={"workspace_ids": ["ws-1"]})
    a = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 7, 7, "median"), p, None, now=T0)
    assert SUPPRESS_PROTECTED in a.suppressions


def test_streak_exige_violacoes_consecutivas():
    p = policy(guards={"min_cu_seconds": 100, "consecutive_breaches": 2, "cooldown_minutes": 0})
    base = Baseline(1000.0, 7, 7, "median")

    primeiro = detect.assess_one(interval(cu=5000.0), base, p, None, now=T0)
    assert primeiro.effective_tier == Tier.ALERT
    assert SUPPRESS_STREAK in primeiro.suppressions

    estado = detect.next_state(primeiro, None, now=T0)
    assert estado.streak == 1

    segundo = detect.assess_one(interval(cu=5000.0), base, p, estado, now=T0)
    assert segundo.effective_tier == Tier.KILL
    assert SUPPRESS_STREAK not in segundo.suppressions


def test_streak_zera_quando_item_normaliza():
    p = policy()
    normal = detect.assess_one(interval(cu=1000.0), Baseline(1000.0, 7, 7, "median"), p, None, now=T0)
    estado = detect.next_state(normal, ItemState("F64", "item-a", streak=5), now=T0)
    assert estado.streak == 0


def test_cooldown_bloqueia_acao_repetida():
    p = policy(guards={"min_cu_seconds": 100, "consecutive_breaches": 1, "cooldown_minutes": 60})
    estado = ItemState("F64", "item-a", streak=3, last_action_ts=T0 - dt.timedelta(minutes=10))
    a = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 7, 7, "median"), p, estado, now=T0)
    assert a.effective_tier == Tier.ALERT
    assert SUPPRESS_COOLDOWN in a.suppressions


def test_cooldown_expira():
    p = policy(guards={"min_cu_seconds": 100, "consecutive_breaches": 1, "cooldown_minutes": 60})
    estado = ItemState("F64", "item-a", streak=3, last_action_ts=T0 - dt.timedelta(minutes=90))
    a = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 7, 7, "median"), p, estado, now=T0)
    assert a.effective_tier == Tier.KILL


def test_freeze_window_desliga_enforcement():
    p = policy(freeze_windows=[{"name": "fechamento", "days_of_month": [22], "start_hour": 0, "end_hour": 24}])
    a = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 7, 7, "median"), p, None, now=T0)
    assert a.effective_tier == Tier.ALERT
    assert any("freeze_window" in s for s in a.suppressions)


def test_freeze_window_fora_da_faixa_nao_afeta():
    p = policy(freeze_windows=[{"name": "madrugada", "start_hour": 1, "end_hour": 6}])
    a = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 7, 7, "median"), p, None, now=T0)
    assert a.effective_tier == Tier.KILL


def test_circuit_breaker_derruba_tudo_quando_muitos_itens_disparam():
    """Vinte itens anômalos ao mesmo tempo é problema da capacidade, não dos itens."""
    p = policy(guards={"min_cu_seconds": 100, "consecutive_breaches": 1, "max_actions_per_run": 5})
    base = Baseline(1000.0, 7, 7, "median")
    avaliados = [
        detect.assess_one(interval(cu=5000.0, item_id=f"i{n}"), base, p, None, now=T0)
        for n in range(20)
    ]
    assert all(a.effective_tier == Tier.KILL for a in avaliados)

    resultado = detect.apply_run_budget(avaliados, p)
    assert all(a.effective_tier == Tier.ALERT for a in resultado)
    assert all(SUPPRESS_RUN_BUDGET in a.suppressions for a in resultado)


def test_circuit_breaker_nao_dispara_abaixo_do_orcamento():
    p = policy(guards={"min_cu_seconds": 100, "consecutive_breaches": 1, "max_actions_per_run": 5})
    base = Baseline(1000.0, 7, 7, "median")
    avaliados = [
        detect.assess_one(interval(cu=5000.0, item_id=f"i{n}"), base, p, None, now=T0) for n in range(3)
    ]
    assert all(a.effective_tier == Tier.KILL for a in detect.apply_run_budget(avaliados, p))


def test_guard_de_utilizacao_da_capacidade():
    p = policy(guards={"min_cu_seconds": 100, "consecutive_breaches": 1,
                       "min_capacity_utilization_percent": 60})
    a = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 7, 7, "median"), p, None, now=T0)
    assert a.effective_tier == Tier.KILL

    contido = detect.apply_capacity_guard([a], 20.0, p)[0]
    assert contido.effective_tier == Tier.ALERT

    a2 = detect.assess_one(interval(cu=5000.0), Baseline(1000.0, 7, 7, "median"), p, None, now=T0)
    assert detect.apply_capacity_guard([a2], 85.0, p)[0].effective_tier == Tier.KILL


def test_item_saudavel_nao_gera_nada():
    a = detect.assess_one(interval(cu=1050.0), Baseline(1000.0, 7, 7, "median"), policy(), None, now=T0)
    assert a.tier == Tier.NONE
    assert a.suppressions == []
