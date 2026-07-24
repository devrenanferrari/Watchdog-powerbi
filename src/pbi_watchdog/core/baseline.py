"""Derivação de intervalos a partir de snapshots cumulativos e cálculo de baseline.

Funções puras: recebem listas de dataclasses, devolvem listas de dataclasses.
Nenhuma depende de Spark, pandas ou rede — é aqui que os testes moram.
"""

from __future__ import annotations

import datetime as dt
import statistics
from typing import Dict, Iterable, List, Optional, Sequence

from ..config import BaselineConfig
from ..models import Baseline, Interval, ItemSnapshot


def bucket_key(when: dt.datetime, mode: str) -> str:
    """Identifica o bucket temporal de uma leitura. Baselines só se comparam dentro do mesmo bucket."""
    if mode == "hour_of_week":
        return f"d{when.weekday()}h{when.hour}"
    return f"h{when.hour}"


def derive_intervals(
    current: Sequence[ItemSnapshot],
    previous: Dict[str, ItemSnapshot],
) -> List[Interval]:
    """Converte snapshots cumulativos do dia em consumo do intervalo.

    `previous` mapeia item_id -> snapshot da execução anterior. Itens sem histórico
    anterior (novos, ou primeira execução) são ignorados: sem os dois pontos não há delta.

    O contador do Metrics App zera à meia-noite. Quando o snapshot anterior é de outro dia,
    ou quando o acumulado caiu, tratamos o valor atual como o consumo do intervalo — é uma
    superestimação limitada ao que passou desde a virada, e preferimos errar para o lado
    de detectar do que de ignorar.
    """
    out: List[Interval] = []
    for snap in current:
        prev = previous.get(snap.item_id)
        if prev is None:
            continue
        if prev.ts >= snap.ts:
            continue

        same_day = prev.ts.date() == snap.ts.date()
        if same_day and snap.cu_seconds_today >= prev.cu_seconds_today:
            delta = snap.cu_seconds_today - prev.cu_seconds_today
        else:
            delta = snap.cu_seconds_today  # virada de dia ou reset do contador

        out.append(
            Interval(
                capacity_key=snap.capacity_key,
                item_id=snap.item_id,
                item_name=snap.item_name,
                item_kind=snap.item_kind,
                workspace_id=snap.workspace_id,
                workspace_name=snap.workspace_name,
                window_start=prev.ts,
                window_end=snap.ts,
                cu_seconds=max(0.0, delta),
            )
        )
    return out


def normalize_per_minute(interval: Interval, target_minutes: float) -> float:
    """Reescala o consumo para uma janela de referência.

    Sem isto, um atraso no agendador (intervalo de 40 min em vez de 15) parece um pico de 2.7x.
    """
    if interval.minutes <= 0:
        return interval.cu_seconds
    return interval.cu_seconds * (target_minutes / interval.minutes)


def is_comparable(interval: Interval, target_minutes: float, max_stretch: float) -> bool:
    """Um intervalo só é comparável se sua duração for próxima da cadência esperada.

    `normalize_per_minute` assume consumo uniforme dentro da janela. Para um atraso de 40 min
    contra 15 esperados, a suposição é aceitável. Para um gap de 12 horas — watchdog parado,
    agendador travado — ela é falsa exatamente no caso que interessa: se houve um pico de
    5 minutos dentro dessas 12 horas, dividir por 48 o esconde, e se não houve, dividir uma
    carga diária inteira produz uma amostra artificialmente baixa que rebaixa a baseline.
    Nos dois sentidos o resultado é lixo, então o intervalo é descartado.
    """
    if interval.minutes <= 0 or target_minutes <= 0:
        return False
    ratio = interval.minutes / target_minutes
    return (1.0 / max_stretch) <= ratio <= max_stretch


def _aggregate(values: List[float], cfg: BaselineConfig) -> float:
    if not values:
        return 0.0
    vals = sorted(values)
    if cfg.trim_top_percent > 0 and len(vals) >= 4:
        keep = max(1, int(len(vals) * (1 - cfg.trim_top_percent / 100.0)))
        vals = vals[:keep]
    if cfg.method == "mean":
        return statistics.fmean(vals)
    if cfg.method == "median":
        return statistics.median(vals)
    quantile = 0.75 if cfg.method == "p75" else 0.90
    idx = min(len(vals) - 1, round(quantile * (len(vals) - 1)))
    return vals[idx]


def compute_baselines(
    history: Iterable[Interval],
    cfg: BaselineConfig,
    *,
    target_bucket: str,
    reference_date: dt.date,
    target_minutes: float,
    max_stretch: Optional[float] = None,
) -> Dict[str, Baseline]:
    """Baseline por item: agregação do consumo histórico no mesmo bucket horário.

    `history` deve conter apenas intervalos anteriores a `reference_date` — o dia corrente
    nunca entra na própria baseline. Cada intervalo é normalizado para `target_minutes`
    antes de agregar, e os de duração incomparável (ver `is_comparable`) são descartados.
    """
    by_item: Dict[str, List[float]] = {}
    days_by_item: Dict[str, set] = {}

    for iv in history:
        if iv.date >= reference_date:
            continue
        if bucket_key(iv.window_end, cfg.bucket) != target_bucket:
            continue
        if (reference_date - iv.date).days > cfg.lookback_days:
            continue
        if max_stretch is not None and not is_comparable(iv, target_minutes, max_stretch):
            continue
        by_item.setdefault(iv.item_id, []).append(normalize_per_minute(iv, target_minutes))
        days_by_item.setdefault(iv.item_id, set()).add(iv.date)

    result: Dict[str, Baseline] = {}
    for item_id, values in by_item.items():
        result[item_id] = Baseline(
            value=_aggregate(values, cfg),
            days=len(days_by_item[item_id]),
            samples=len(values),
            method=cfg.method,
        )
    return result


def capacity_totals(intervals: Sequence[Interval]) -> float:
    return sum(iv.cu_seconds for iv in intervals)


def latest_snapshot_map(snapshots: Iterable[ItemSnapshot]) -> Dict[str, ItemSnapshot]:
    """Reduz um histórico de snapshots ao mais recente por item."""
    latest: Dict[str, ItemSnapshot] = {}
    for s in snapshots:
        cur: Optional[ItemSnapshot] = latest.get(s.item_id)
        if cur is None or s.ts > cur.ts:
            latest[s.item_id] = s
    return latest
