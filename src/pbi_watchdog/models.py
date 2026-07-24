"""Tipos de domínio. Sem I/O, sem dependência de runtime — tudo que o core manipula."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional


class Tier(IntEnum):
    """Degraus de severidade. Ordenáveis: Tier.KILL > Tier.ALERT."""

    NONE = 0
    ALERT = 1
    THROTTLE = 2
    KILL = 3

    @property
    def label(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class ItemSnapshot:
    """CU acumulado do dia para um item, no instante `ts`. É o que a fonte de métricas devolve."""

    ts: dt.datetime
    capacity_key: str
    capacity_id: str
    item_id: str
    item_name: str
    item_kind: str
    workspace_id: str
    workspace_name: str
    cu_seconds_today: float

    @property
    def date(self) -> dt.date:
        return self.ts.date()

    @property
    def hour(self) -> int:
        return self.ts.hour

    @property
    def dow(self) -> int:
        return self.ts.weekday()


@dataclass(frozen=True)
class Interval:
    """Consumo derivado entre dois snapshots consecutivos do mesmo item."""

    capacity_key: str
    item_id: str
    item_name: str
    item_kind: str
    workspace_id: str
    workspace_name: str
    window_start: dt.datetime
    window_end: dt.datetime
    cu_seconds: float

    @property
    def date(self) -> dt.date:
        return self.window_end.date()

    @property
    def hour(self) -> int:
        return self.window_end.hour

    @property
    def dow(self) -> int:
        return self.window_end.weekday()

    @property
    def minutes(self) -> float:
        return (self.window_end - self.window_start).total_seconds() / 60.0


@dataclass(frozen=True)
class Baseline:
    """Consumo esperado para um item num bucket horário, derivado do histórico."""

    value: float
    days: int
    samples: int
    method: str

    @property
    def trustworthy(self) -> bool:
        return self.days > 0 and self.value > 0


#: Motivos pelos quais uma anomalia detectada não vira ação.
SUPPRESS_BELOW_MIN_CU = "below_min_cu"
SUPPRESS_INSUFFICIENT_BASELINE = "insufficient_baseline"
SUPPRESS_PROTECTED = "protected"
SUPPRESS_COOLDOWN = "cooldown"
SUPPRESS_STREAK = "awaiting_consecutive_breaches"
SUPPRESS_FREEZE = "freeze_window"
SUPPRESS_OBSERVE_MODE = "observe_mode"
SUPPRESS_RUN_BUDGET = "max_actions_per_run"


@dataclass
class Assessment:
    """Veredito do core para um item num ciclo. `tier` é o que foi detectado;
    `effective_tier` é o que sobrou depois das travas de segurança."""

    interval: Interval
    baseline: Optional[Baseline]
    ratio: Optional[float]
    tier: Tier = Tier.NONE
    effective_tier: Tier = Tier.NONE
    streak: int = 0
    suppressions: List[str] = field(default_factory=list)

    @property
    def item_id(self) -> str:
        return self.interval.item_id

    @property
    def capacity_key(self) -> str:
        return self.interval.capacity_key

    @property
    def suppressed(self) -> bool:
        return self.tier > Tier.NONE and self.effective_tier < self.tier

    def suppress(self, reason: str, to: Tier = Tier.NONE) -> None:
        if reason not in self.suppressions:
            self.suppressions.append(reason)
        self.effective_tier = min(self.effective_tier, to)


@dataclass
class ActionResult:
    action: str
    ok: bool
    detail: str = ""
    targets: List[str] = field(default_factory=list)


@dataclass
class Event:
    """Linha de auditoria. Tudo que o watchdog decide vira um Event — inclusive o que ele
    decidiu NÃO fazer, e por quê (`suppressions`). Sem isso não há como calibrar: você vê
    o que aconteceu, mas não o que quase aconteceu."""

    ts: dt.datetime
    run_id: str
    capacity_key: str
    mode: str
    item_id: str
    item_name: str
    item_kind: str
    workspace_id: str
    tier: str
    effective_tier: str
    ratio: Optional[float]
    cu_seconds: float
    baseline_cu_seconds: Optional[float]
    baseline_days: int
    streak: int
    suppressions: str
    actions: str
    detail: str = ""

    @classmethod
    def from_assessment(
        cls,
        a: Assessment,
        run_id: str,
        mode: str,
        results: Optional[List[ActionResult]] = None,
        ts: Optional[dt.datetime] = None,
    ) -> "Event":
        results = results or []
        return cls(
            ts=ts or a.interval.window_end,
            run_id=run_id,
            capacity_key=a.capacity_key,
            mode=mode,
            item_id=a.item_id,
            item_name=a.interval.item_name,
            item_kind=a.interval.item_kind,
            workspace_id=a.interval.workspace_id,
            tier=a.tier.label,
            effective_tier=a.effective_tier.label,
            ratio=a.ratio,
            cu_seconds=a.interval.cu_seconds,
            baseline_cu_seconds=a.baseline.value if a.baseline else None,
            baseline_days=a.baseline.days if a.baseline else 0,
            streak=a.streak,
            suppressions=",".join(a.suppressions),
            actions=",".join(f"{r.action}:{'ok' if r.ok else 'fail'}" for r in results),
            detail="; ".join(r.detail for r in results if r.detail),
        )


@dataclass
class ItemState:
    """Estado persistido entre execuções: sequência de violações e cooldown de ação."""

    capacity_key: str
    item_id: str
    streak: int = 0
    last_tier: str = "none"
    last_action_ts: Optional[dt.datetime] = None
    last_seen_ts: Optional[dt.datetime] = None


@dataclass
class RunSummary:
    run_id: str
    started_at: dt.datetime
    capacity_key: str
    mode: str
    items_scanned: int = 0
    anomalies: int = 0
    actions_taken: int = 0
    errors: List[str] = field(default_factory=list)
    events: List[Event] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)
