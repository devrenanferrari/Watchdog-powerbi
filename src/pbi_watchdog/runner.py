"""Orquestração de um ciclo do watchdog.

Um ciclo, por capacidade:
  snapshot -> derivar intervalos -> baseline -> avaliar -> travas de ciclo -> agir -> auditar
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from typing import Dict, List, Optional, Sequence

from . import clock
from .actions import build_registry
from .auth import build_token_provider
from .config import CapacityConfig, WatchdogConfig
from .core import baseline as bl
from .core import detect
from .models import ActionResult, Assessment, Event, RunSummary, Tier
from .notify import broadcast, build_notifiers, format_message
from .rest import RestClient
from .sources import build_source
from .storage import build_store

log = logging.getLogger(__name__)


def _now(cfg: WatchdogConfig) -> dt.datetime:
    return clock.now_in(cfg.timezone)


class Watchdog:
    def __init__(self, config: WatchdogConfig, *, dry_run: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run

        # O provider é sempre construído: mesmo com a fonte via sempy, as ações de
        # cancelamento falam REST. A aquisição de token é preguiçosa, então isto não
        # custa nada quando nenhuma chamada acontece.
        self.tokens = build_token_provider(config.auth)
        self.client: Optional[RestClient] = RestClient(self.tokens)
        self.source = build_source(config.metrics_source, self.client)
        self.store = build_store(config.storage)
        self.actions = build_registry(self.client)

    # ------------------------------------------------------------------ ciclo

    def run_once(self, capacity_keys: Optional[Sequence[str]] = None) -> List[RunSummary]:
        run_id = uuid.uuid4().hex[:12]
        now = _now(self.config)
        caps = [
            c
            for c in self.config.enabled_capacities
            if capacity_keys is None or c.key in capacity_keys
        ]
        summaries = []
        for cap in caps:
            try:
                summaries.append(self.run_capacity(cap, run_id=run_id, now=now))
            except Exception as e:
                # Uma capacidade quebrada (permissão revogada, DAX incompatível) não pode
                # cegar o watchdog para as demais.
                log.exception("Falha na capacidade %s", cap.key)
                summaries.append(
                    RunSummary(
                        run_id=run_id,
                        started_at=now,
                        capacity_key=cap.key,
                        mode=cap.policy.mode if cap.policy else "observe",
                        errors=[str(e)],
                    )
                )
        return summaries

    def run_capacity(self, cap: CapacityConfig, *, run_id: str, now: dt.datetime) -> RunSummary:
        policy = cap.policy
        assert policy is not None, "config.resolve() não rodou"
        summary = RunSummary(run_id=run_id, started_at=now, capacity_key=cap.key, mode=policy.mode)

        # 1. snapshot cumulativo do dia
        snapshots = self.source.snapshot(cap, now=now)
        summary.items_scanned = len(snapshots)
        if not snapshots:
            log.info("[%s] nenhum item retornado pela fonte de métricas.", cap.key)
            return summary

        previous = self.store.previous_snapshots(cap.key, before=now)
        self.store.write_snapshots(snapshots)

        # 2. intervalos
        intervals = bl.derive_intervals(snapshots, previous)
        if not intervals:
            log.info("[%s] primeiro snapshot — sem intervalo anterior para comparar.", cap.key)
            summary.extra["bootstrap"] = True
            return summary
        self.store.write_intervals(intervals)

        # Intervalos de duração destoante entram no histórico bruto mas não são avaliados:
        # um gap no agendador não pode virar anomalia nem rebaixar a baseline.
        stretch = policy.guards.max_interval_stretch
        comparable = [
            iv for iv in intervals if bl.is_comparable(iv, policy.interval_minutes, stretch)
        ]
        skipped = len(intervals) - len(comparable)
        if skipped:
            summary.extra["skipped_stretched_intervals"] = skipped
            log.info(
                "[%s] %s intervalo(s) descartado(s) por duração fora de %sx de %s min.",
                cap.key, skipped, stretch, policy.interval_minutes,
            )
        if not comparable:
            return summary
        intervals = comparable

        # 3. baseline no mesmo bucket
        target_bucket = bl.bucket_key(now, policy.baseline.bucket)
        hours = [now.hour]
        history = self.store.load_intervals(
            cap.key,
            since=now.date() - dt.timedelta(days=policy.baseline.lookback_days),
            until=now.date(),
            hours=hours,
        )
        baselines = bl.compute_baselines(
            history,
            policy.baseline,
            target_bucket=target_bucket,
            reference_date=now.date(),
            target_minutes=policy.interval_minutes,
            max_stretch=stretch,
        )

        # 4. avaliação + travas de ciclo
        states = self.store.load_states(cap.key)
        assessments = detect.assess_all(intervals, baselines, policy, states, now=now)
        assessments = detect.apply_capacity_guard(
            assessments, self._capacity_utilization(cap, intervals), policy
        )
        assessments = detect.apply_run_budget(assessments, policy)

        # 5. ação + auditoria
        notifiers = build_notifiers(policy.notify)
        events: List[Event] = []
        new_states = []
        for a in assessments:
            new_states.append(detect.next_state(a, states.get(a.item_id), now=now))
            if a.tier == Tier.NONE:
                continue
            summary.anomalies += 1

            results = self._execute(a, policy)
            if any(r.action != "notify" and r.ok and r.targets for r in results):
                summary.actions_taken += 1

            broadcast(notifiers, format_message(a, policy.mode, results), a.tier)
            events.append(Event.from_assessment(a, run_id, policy.mode, results, ts=now))

        # Registra também os itens saudáveis? Não: o volume mataria a tabela. Só anomalias
        # e o resumo do ciclo entram na auditoria.
        if events:
            self.store.write_events(events)
        self.store.save_states(new_states)
        summary.events = events

        log.info(
            "[%s] %s itens, %s anomalias, %s ações (modo=%s, dry_run=%s)",
            cap.key, summary.items_scanned, summary.anomalies, summary.actions_taken,
            policy.mode, self.dry_run,
        )
        return summary

    # ------------------------------------------------------------------ auxiliares

    def _execute(self, a: Assessment, policy) -> List[ActionResult]:
        """Executa as ações do tier efetivo. `notify` é tratada pelo runner, não pelo registry."""
        if a.effective_tier == Tier.NONE:
            return []
        names = getattr(policy.actions, a.effective_tier.label, [])
        results: List[ActionResult] = []
        for name in names:
            if name == "notify":
                continue
            action = self.actions.get(name)
            if action is None:
                results.append(ActionResult(name, False, detail="ação não registrada"))
                continue
            try:
                results.append(action.execute(a, dry_run=self.dry_run))
            except Exception as e:
                # Ação quebrada vira registro de auditoria, não aborta o ciclo nem as
                # ações seguintes do mesmo item.
                log.exception("Ação %s falhou em %s", name, a.item_id)
                results.append(ActionResult(name, False, detail=str(e)))
        return results

    def _capacity_utilization(self, cap: CapacityConfig, intervals: Sequence) -> Optional[float]:
        """Utilização da capacidade no intervalo, em % do orçamento de CU.

        Só é calculável quando o SKU está declarado na config: CU·s disponíveis no intervalo
        = cu_por_segundo_do_sku * duração. Sem SKU, a trava de utilização fica inativa.
        """
        if not cap.sku:
            return None
        cu_per_second = _SKU_CU.get(cap.sku.upper())
        if not cu_per_second or not intervals:
            return None
        minutes = max(iv.minutes for iv in intervals)
        budget = cu_per_second * minutes * 60
        if budget <= 0:
            return None
        return 100.0 * bl.capacity_totals(intervals) / budget

    def close(self) -> None:
        self.store.close()


#: Capacity Units por segundo de cada SKU. Usado apenas pela trava de utilização.
_SKU_CU: Dict[str, float] = {
    "F2": 2, "F4": 4, "F8": 8, "F16": 16, "F32": 32, "F64": 64,
    "F128": 128, "F256": 256, "F512": 512, "F1024": 1024, "F2048": 2048,
    "A1": 1, "A2": 2, "A3": 4, "A4": 8, "A5": 16, "A6": 32, "A7": 64, "A8": 128,
    "P1": 8, "P2": 16, "P3": 32, "P4": 64, "P5": 128,
    "EM1": 1, "EM2": 2, "EM3": 4,
}
