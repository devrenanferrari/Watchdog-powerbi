"""Motor de decisão: intervalo + baseline + política -> Assessment.

Puro e determinístico. Recebe o estado persistido como argumento em vez de ir buscá-lo,
para que o comportamento sob streak/cooldown seja testável linha a linha.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional, Sequence

from ..config import FreezeWindow, PolicyConfig, ProtectConfig
from ..models import (
    SUPPRESS_BELOW_MIN_CU,
    SUPPRESS_COOLDOWN,
    SUPPRESS_FREEZE,
    SUPPRESS_INSUFFICIENT_BASELINE,
    SUPPRESS_OBSERVE_MODE,
    SUPPRESS_PROTECTED,
    SUPPRESS_RUN_BUDGET,
    SUPPRESS_STREAK,
    Assessment,
    Baseline,
    Interval,
    ItemState,
    Tier,
)
from .baseline import normalize_per_minute


def classify(ratio: Optional[float], policy: PolicyConfig) -> Tier:
    if ratio is None:
        return Tier.NONE
    t = policy.thresholds
    if ratio >= t.kill:
        return Tier.KILL
    if ratio >= t.throttle:
        return Tier.THROTTLE
    if ratio >= t.alert:
        return Tier.ALERT
    return Tier.NONE


def is_protected(interval: Interval, protect: ProtectConfig) -> bool:
    if interval.item_id in protect.item_ids:
        return True
    if interval.workspace_id in protect.workspace_ids:
        return True
    if interval.item_kind in protect.item_kinds:
        return True
    for pattern in protect.name_patterns:
        if re.search(pattern, interval.item_name or "", re.IGNORECASE):
            return True
    return False


def in_freeze_window(when: dt.datetime, windows: Sequence[FreezeWindow]) -> Optional[FreezeWindow]:
    for w in windows:
        if w.days and when.weekday() not in w.days:
            continue
        if w.days_of_month and when.day not in w.days_of_month:
            continue
        if w.start_hour <= when.hour < w.end_hour:
            return w
    return None


def assess_one(
    interval: Interval,
    baseline: Optional[Baseline],
    policy: PolicyConfig,
    state: Optional[ItemState],
    *,
    now: dt.datetime,
) -> Assessment:
    """Avalia um item. Não decide ainda se a ação cabe no orçamento do ciclo — isso é `apply_run_budget`."""
    normalized = normalize_per_minute(interval, policy.interval_minutes)
    ratio = (normalized / baseline.value) if (baseline and baseline.trustworthy) else None

    tier = classify(ratio, policy)
    a = Assessment(
        interval=interval,
        baseline=baseline,
        ratio=ratio,
        tier=tier,
        effective_tier=tier,
        streak=(state.streak if state else 0),
    )

    if tier == Tier.NONE:
        return a

    # --- travas que derrubam para observação pura -----------------------------
    if baseline is None or baseline.days < policy.baseline.min_days:
        a.suppress(SUPPRESS_INSUFFICIENT_BASELINE)
        return a

    if normalized < policy.guards.min_cu_seconds:
        a.suppress(SUPPRESS_BELOW_MIN_CU)
        return a

    # A partir daqui a anomalia é real; o alerta sempre passa. As travas seguintes
    # limitam quanto de AÇÃO ela gera, nunca o alerta.
    if is_protected(interval, policy.protect):
        a.suppress(SUPPRESS_PROTECTED, to=Tier.ALERT)

    if policy.mode != "enforce":
        a.suppress(SUPPRESS_OBSERVE_MODE, to=Tier.ALERT)

    frozen = in_freeze_window(now, policy.freeze_windows)
    if frozen is not None:
        a.suppress(f"{SUPPRESS_FREEZE}:{frozen.name}", to=Tier.ALERT)

    if state and state.last_action_ts and policy.guards.cooldown_minutes:
        elapsed = (now - state.last_action_ts).total_seconds() / 60.0
        if elapsed < policy.guards.cooldown_minutes:
            a.suppress(SUPPRESS_COOLDOWN, to=Tier.ALERT)

    if a.streak + 1 < policy.guards.consecutive_breaches:
        a.suppress(SUPPRESS_STREAK, to=Tier.ALERT)

    return a


def next_state(assessment: Assessment, state: Optional[ItemState], *, now: dt.datetime) -> ItemState:
    """Avança o estado persistido. Streak sobe enquanto o item viola, zera quando normaliza."""
    st = state or ItemState(capacity_key=assessment.capacity_key, item_id=assessment.item_id)
    if assessment.tier >= Tier.ALERT:
        st.streak += 1
    else:
        st.streak = 0
    st.last_tier = assessment.tier.label
    st.last_seen_ts = now
    if assessment.effective_tier >= Tier.THROTTLE:
        st.last_action_ts = now
    return st


def assess_all(
    intervals: Sequence[Interval],
    baselines: Dict[str, Baseline],
    policy: PolicyConfig,
    states: Dict[str, ItemState],
    *,
    now: dt.datetime,
) -> List[Assessment]:
    out = [
        assess_one(iv, baselines.get(iv.item_id), policy, states.get(iv.item_id), now=now)
        for iv in intervals
    ]
    out.sort(key=lambda a: (a.tier, a.ratio or 0.0), reverse=True)
    return out


def apply_run_budget(assessments: Sequence[Assessment], policy: PolicyConfig) -> List[Assessment]:
    """Circuit breaker: um pico que atinge muitos itens ao mesmo tempo raramente é culpa deles.

    É sintoma de algo sistêmico (capacidade sobrecarregada, incidente no serviço, mudança de
    baseline). Matar 40 itens nesse cenário transforma um problema de performance num apagão.
    Acima do orçamento, tudo cai para alerta.
    """
    actionable = [a for a in assessments if a.effective_tier >= Tier.THROTTLE]
    if len(actionable) > policy.guards.max_actions_per_run:
        for a in actionable:
            a.suppress(SUPPRESS_RUN_BUDGET, to=Tier.ALERT)
    return list(assessments)


def apply_capacity_guard(
    assessments: Sequence[Assessment],
    utilization_percent: Optional[float],
    policy: PolicyConfig,
) -> List[Assessment]:
    """Um item a 3x a baseline dele numa capacidade a 20% de uso não está machucando ninguém."""
    floor = policy.guards.min_capacity_utilization_percent
    if floor is None or utilization_percent is None:
        return list(assessments)
    if utilization_percent >= floor:
        return list(assessments)
    for a in assessments:
        if a.effective_tier >= Tier.THROTTLE:
            a.suppress(f"capacity_utilization_below_{floor}", to=Tier.ALERT)
    return list(assessments)
