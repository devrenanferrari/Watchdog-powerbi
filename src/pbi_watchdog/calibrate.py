"""Calibração: usa o histórico coletado em OBSERVE para sugerir thresholds e guards.

A pergunta que a fase de observação existe para responder é: *com estes limiares, quantas
vezes eu teria matado alguma coisa na semana passada, e quais?* Isto responde com números.
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from .config import CapacityConfig, WatchdogConfig
from .core import baseline as bl
from .core.detect import classify
from .models import Interval, Tier
from .storage import build_store


@dataclass
class ItemProfile:
    item_id: str
    item_name: str
    item_kind: str
    samples: int
    median_cu: float
    p95_cu: float
    max_ratio: float
    breaches: Dict[str, int] = field(default_factory=dict)


@dataclass
class CalibrationReport:
    capacity_key: str
    days_of_history: int
    total_intervals: int
    items: List[ItemProfile]
    would_alert: int
    would_throttle: int
    would_kill: int
    suggested_alert: float
    suggested_throttle: float
    suggested_kill: float
    suggested_min_cu: float
    noisy_items: List[str]
    notes: List[str] = field(default_factory=list)


def _percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, max(0, round(q * (len(s) - 1))))
    return s[idx]


def calibrate_capacity(
    config: WatchdogConfig, cap: CapacityConfig, *, days: int = 14
) -> CalibrationReport:
    policy = cap.policy
    assert policy is not None
    store = build_store(config.storage)
    try:
        today = dt.date.today()
        intervals = store.load_intervals(
            cap.key, since=today - dt.timedelta(days=days), until=today + dt.timedelta(days=1)
        )
    finally:
        store.close()

    notes: List[str] = []
    if not intervals:
        return CalibrationReport(
            cap.key, 0, 0, [], 0, 0, 0,
            policy.thresholds.alert, policy.thresholds.throttle, policy.thresholds.kill,
            policy.guards.min_cu_seconds, [],
            notes=["Sem histórico. Rode em observe por pelo menos uma semana antes de calibrar."],
        )

    # Mesmo filtro do runner: o replay precisa ver o que o watchdog teria visto.
    stretch = policy.guards.max_interval_stretch
    descartados = len(intervals)
    intervals = [
        iv for iv in intervals if bl.is_comparable(iv, policy.interval_minutes, stretch)
    ]
    descartados -= len(intervals)
    if descartados:
        notes.append(
            f"{descartados} intervalo(s) descartado(s) por duração fora de {stretch}x de "
            f"{policy.interval_minutes} min — normalmente gaps do agendador."
        )

    by_item: Dict[str, List[Interval]] = {}
    for iv in intervals:
        by_item.setdefault(iv.item_id, []).append(iv)

    # Replay: para cada intervalo, qual seria a razão contra a baseline dos dias anteriores?
    ratios_by_item: Dict[str, List[float]] = {}
    all_ratios: List[float] = []
    for item_id, ivs in by_item.items():
        ivs.sort(key=lambda x: x.window_end)
        for iv in ivs:
            bucket = bl.bucket_key(iv.window_end, policy.baseline.bucket)
            history = [
                h
                for h in ivs
                if h.date < iv.date
                and bl.bucket_key(h.window_end, policy.baseline.bucket) == bucket
                and (iv.date - h.date).days <= policy.baseline.lookback_days
            ]
            if len({h.date for h in history}) < policy.baseline.min_days:
                continue
            base = bl.compute_baselines(
                history, policy.baseline,
                target_bucket=bucket, reference_date=iv.date,
                target_minutes=policy.interval_minutes, max_stretch=stretch,
            ).get(item_id)
            if not base or not base.trustworthy:
                continue
            normalized = bl.normalize_per_minute(iv, policy.interval_minutes)
            if normalized < policy.guards.min_cu_seconds:
                continue
            ratio = normalized / base.value
            ratios_by_item.setdefault(item_id, []).append(ratio)
            all_ratios.append(ratio)

    profiles: List[ItemProfile] = []
    for item_id, ivs in by_item.items():
        cus = [bl.normalize_per_minute(i, policy.interval_minutes) for i in ivs]
        ratios = ratios_by_item.get(item_id, [])
        breaches = {
            t.label: sum(1 for r in ratios if classify(r, policy) >= t)
            for t in (Tier.ALERT, Tier.THROTTLE, Tier.KILL)
        }
        profiles.append(
            ItemProfile(
                item_id=item_id,
                item_name=ivs[0].item_name,
                item_kind=ivs[0].item_kind,
                samples=len(ivs),
                median_cu=statistics.median(cus) if cus else 0.0,
                p95_cu=_percentile(cus, 0.95),
                max_ratio=max(ratios) if ratios else 0.0,
                breaches=breaches,
            )
        )
    profiles.sort(key=lambda p: p.breaches.get("kill", 0) * 100 + p.max_ratio, reverse=True)

    would_alert = sum(1 for r in all_ratios if classify(r, policy) >= Tier.ALERT)
    would_throttle = sum(1 for r in all_ratios if classify(r, policy) >= Tier.THROTTLE)
    would_kill = sum(1 for r in all_ratios if classify(r, policy) >= Tier.KILL)

    # Sugestões a partir da distribuição observada: alerta no p95, throttle no p99,
    # kill acima disso. O objetivo é um punhado de alertas por semana, não centenas.
    if all_ratios:
        sug_alert = round(max(1.2, _percentile(all_ratios, 0.95)), 2)
        sug_throttle = round(max(sug_alert + 0.2, _percentile(all_ratios, 0.99)), 2)
        sug_kill = round(max(sug_throttle + 0.3, _percentile(all_ratios, 0.995)), 2)
    else:
        sug_alert = policy.thresholds.alert
        sug_throttle = policy.thresholds.throttle
        sug_kill = policy.thresholds.kill

    all_cu = [bl.normalize_per_minute(i, policy.interval_minutes) for i in intervals]
    sug_min_cu = round(_percentile(all_cu, 0.50), 0)

    noisy = [p.item_name for p in profiles if p.breaches.get("throttle", 0) >= 3][:10]
    if noisy:
        notes.append(
            "Itens que disparariam ação 3+ vezes na janela analisada são candidatos a "
            "`protect.item_ids` ou a thresholds próprios — normalmente são cargas legitimamente "
            "irregulares, não abuso."
        )
    days_seen = len({i.date for i in intervals})
    if days_seen < 7:
        notes.append(f"Só {days_seen} dia(s) de histórico; as sugestões ficam instáveis abaixo de 7.")

    return CalibrationReport(
        capacity_key=cap.key,
        days_of_history=days_seen,
        total_intervals=len(intervals),
        items=profiles,
        would_alert=would_alert,
        would_throttle=would_throttle,
        would_kill=would_kill,
        suggested_alert=sug_alert,
        suggested_throttle=sug_throttle,
        suggested_kill=sug_kill,
        suggested_min_cu=sug_min_cu,
        noisy_items=noisy,
        notes=notes,
    )


def render(report: CalibrationReport, *, top: int = 15) -> str:
    L = [
        f"Calibração — capacidade '{report.capacity_key}'",
        f"  Histórico: {report.days_of_history} dia(s), {report.total_intervals} intervalos",
        "",
        "Com os thresholds ATUAIS, na janela analisada:",
        f"  alertas:  {report.would_alert}",
        f"  throttle: {report.would_throttle}",
        f"  kill:     {report.would_kill}",
        "",
        "Sugestão a partir da distribuição observada:",
        f"  thresholds: alert={report.suggested_alert}  throttle={report.suggested_throttle}  "
        f"kill={report.suggested_kill}",
        f"  guards.min_cu_seconds: {report.suggested_min_cu:.0f}",
        "",
        f"Itens mais expostos (top {top}):",
        f"  {'item':<42} {'kind':<16} {'p95 CU·s':>10} {'max ratio':>10} {'alert/thr/kill':>16}",
    ]
    for p in report.items[:top]:
        b = p.breaches
        L.append(
            f"  {p.item_name[:42]:<42} {p.item_kind[:16]:<16} {p.p95_cu:>10,.0f} "
            f"{p.max_ratio:>10.2f} {b.get('alert',0):>4}/{b.get('throttle',0):>4}/{b.get('kill',0):>4}"
        )
    if report.noisy_items:
        L += ["", "Candidatos a protect.item_ids (ruidosos):"] + [f"  - {n}" for n in report.noisy_items]
    if report.notes:
        L += [""] + [f"! {n}" for n in report.notes]
    return "\n".join(L)


def calibrate_all(config: WatchdogConfig, *, days: int = 14, capacity_keys: Optional[Sequence[str]] = None):
    caps = [c for c in config.enabled_capacities if capacity_keys is None or c.key in capacity_keys]
    return [calibrate_capacity(config, c, days=days) for c in caps]
