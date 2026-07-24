"""Fontes de métrica de consumo. Todas devolvem `List[ItemSnapshot]` para uma capacidade."""

from __future__ import annotations

import datetime as dt
import logging
from typing import List, Optional, Protocol

from ..config import CapacityConfig, MetricsSourceConfig
from ..models import ItemSnapshot
from ..rest import RestClient
from . import profiles as prof

log = logging.getLogger(__name__)


class MetricsSource(Protocol):
    def snapshot(self, capacity: CapacityConfig, *, now: dt.datetime) -> List[ItemSnapshot]: ...
    def describe(self) -> str: ...


class MetricsAppRestSource:
    """Lê o Capacity Metrics App via `executeQueries` (REST). Roda em qualquer lugar."""

    def __init__(self, cfg: MetricsSourceConfig, client: RestClient) -> None:
        self.cfg = cfg
        self.client = client
        self._profile: Optional[prof.MetricsProfile] = None
        if cfg.profile not in ("auto",) and cfg.profile in prof.PROFILES:
            self._profile = prof.PROFILES[cfg.profile]

    # ------------------------------------------------------------ introspecção

    def _introspect(self) -> tuple:
        tables = self.client.execute_dax(
            self.cfg.workspace_id, self.cfg.dataset_id, prof.DAX_LIST_TABLES
        )
        columns = self.client.execute_dax(
            self.cfg.workspace_id, self.cfg.dataset_id, prof.DAX_LIST_COLUMNS
        )
        return tables, columns

    def list_tables(self) -> List[str]:
        tables, _ = self._introspect()
        return prof.table_names(tables)

    def list_columns(self) -> List[str]:
        tables, columns = self._introspect()
        return prof.join_tables_and_columns(tables, columns)

    def resolve_profile(self) -> prof.MetricsProfile:
        if self._profile is not None:
            return self._profile
        if self.cfg.dax_override:
            self._profile = prof.MetricsProfile(
                name="custom",
                description="dax_override da config",
                requires_tables=[],
                requires_columns=[],
                dax=self.cfg.dax_override,
                column_map=prof.FABRIC_METRICS_V1.column_map,
            )
            return self._profile

        raw_tables, raw_columns = self._introspect()
        tables = prof.table_names(raw_tables)
        columns = prof.join_tables_and_columns(raw_tables, raw_columns)
        picked = prof.pick_profile(tables, columns)
        if picked is None:
            detail = "\n".join(
                f"  - {name}: falta "
                + ", ".join(prof.missing_requirements(prof.PROFILES[name], tables, columns))
                for name in prof.PROBE_ORDER
            )
            raise RuntimeError(
                "Nenhum perfil de DAX bate com o seu Capacity Metrics App.\n"
                f"Tabelas encontradas: {sorted(tables)[:30]}\n"
                f"{detail}\n"
                "Ajuste `metrics_source.dax_override` na config com uma query que devolva "
                f"{prof.REQUIRED_COLUMNS}."
            )
        log.info("Perfil de métricas detectado: %s", picked.name)
        self._profile = picked
        return picked

    # ------------------------------------------------------------ snapshot

    def snapshot(self, capacity: CapacityConfig, *, now: dt.datetime) -> List[ItemSnapshot]:
        profile = self.resolve_profile()
        dax = profile.dax.format(capacity_id=capacity.id)
        rows = self.client.execute_dax(
            self.cfg.workspace_id,
            self.cfg.dataset_id,
            dax,
            timeout=self.cfg.timeout_seconds,
        )
        out: List[ItemSnapshot] = []
        for raw in rows:
            r = prof.normalize_row(raw, profile)
            item_id = r.get("item_id")
            if not item_id:
                continue
            try:
                cu = float(r.get("cu_seconds_today") or 0.0)
            except (TypeError, ValueError):
                continue
            out.append(
                ItemSnapshot(
                    ts=now,
                    capacity_key=capacity.key,
                    capacity_id=capacity.id,
                    item_id=str(item_id),
                    item_name=str(r.get("item_name") or item_id),
                    item_kind=str(r.get("item_kind") or "Unknown"),
                    workspace_id=str(r.get("workspace_id") or ""),
                    workspace_name=str(r.get("workspace_name") or ""),
                    cu_seconds_today=cu,
                )
            )
        return out

    def describe(self) -> str:
        return f"metrics_app_rest(dataset={self.cfg.dataset_id}, profile={self.cfg.profile})"


class MetricsAppSempySource:
    """Mesma leitura, porém via sempy — para quem roda dentro do notebook Fabric e prefere
    a identidade do notebook em vez de habilitar executeQueries no tenant."""

    def __init__(self, cfg: MetricsSourceConfig) -> None:
        self.cfg = cfg
        self._profile: Optional[prof.MetricsProfile] = None

    def _fabric(self):
        try:
            import sempy.fabric as fabric  # type: ignore
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "metrics_source.kind='metrics_app_sempy' exige o extra: "
                "`pip install pbi-watchdog[fabric]` dentro do runtime Fabric."
            ) from e
        return fabric

    def resolve_profile(self) -> prof.MetricsProfile:
        if self._profile is not None:
            return self._profile
        if self.cfg.profile in prof.PROFILES:
            self._profile = prof.PROFILES[self.cfg.profile]
            return self._profile
        fabric = self._fabric()
        tables = list(
            fabric.list_tables(dataset=self.cfg.dataset_name, workspace=self.cfg.workspace_name)["Name"]
        )
        cols_df = fabric.list_columns(dataset=self.cfg.dataset_name, workspace=self.cfg.workspace_name)
        columns = [f"{t}[{c}]" for t, c in zip(cols_df["Table Name"], cols_df["Column Name"])]
        picked = prof.pick_profile(tables, columns)
        if picked is None:
            raise RuntimeError(
                f"Nenhum perfil bate. Tabelas: {sorted(tables)[:30]}. "
                "Use metrics_source.dax_override."
            )
        self._profile = picked
        return picked

    def snapshot(self, capacity: CapacityConfig, *, now: dt.datetime) -> List[ItemSnapshot]:
        fabric = self._fabric()
        profile = self.resolve_profile()
        df = fabric.evaluate_dax(
            dataset=self.cfg.dataset_name,
            workspace=self.cfg.workspace_name,
            dax_string=profile.dax.format(capacity_id=capacity.id),
        )
        out: List[ItemSnapshot] = []
        for _, raw in df.iterrows():
            r = prof.normalize_row(dict(raw), profile)
            if not r.get("item_id"):
                continue
            out.append(
                ItemSnapshot(
                    ts=now,
                    capacity_key=capacity.key,
                    capacity_id=capacity.id,
                    item_id=str(r["item_id"]),
                    item_name=str(r.get("item_name") or r["item_id"]),
                    item_kind=str(r.get("item_kind") or "Unknown"),
                    workspace_id=str(r.get("workspace_id") or ""),
                    workspace_name=str(r.get("workspace_name") or ""),
                    cu_seconds_today=float(r.get("cu_seconds_today") or 0.0),
                )
            )
        return out

    def describe(self) -> str:
        return f"metrics_app_sempy(dataset={self.cfg.dataset_name})"


class FakeSource:
    """Gera consumo sintético. Serve para `pbi-watchdog demo` e para os testes end-to-end."""

    def __init__(self, plan: Optional[dict] = None) -> None:
        self.plan = plan or {}
        self._acc: dict = {}

    def snapshot(self, capacity: CapacityConfig, *, now: dt.datetime) -> List[ItemSnapshot]:
        import random

        items = self.plan.get(capacity.key) or [
            {"id": "item-a", "name": "Vendas Diário", "kind": "SemanticModel", "base": 800},
            {"id": "item-b", "name": "ANTT Regulatório", "kind": "SemanticModel", "base": 400},
            {"id": "item-c", "name": "Pipeline Ingestão", "kind": "DataPipeline", "base": 1200},
        ]
        out = []
        for it in items:
            key = (capacity.key, it["id"])
            spike = it.get("spike_at_hour")
            factor = it.get("spike_factor", 4.0) if spike == now.hour else 1.0
            self._acc[key] = self._acc.get(key, 0.0) + it["base"] * factor * random.uniform(0.9, 1.1)
            out.append(
                ItemSnapshot(
                    ts=now,
                    capacity_key=capacity.key,
                    capacity_id=capacity.id,
                    item_id=it["id"],
                    item_name=it["name"],
                    item_kind=it["kind"],
                    workspace_id=it.get("workspace_id", "ws-demo"),
                    workspace_name=it.get("workspace_name", "Workspace Demo"),
                    cu_seconds_today=self._acc[key],
                )
            )
        return out

    def describe(self) -> str:
        return "fake(sintético)"


def build_source(cfg: MetricsSourceConfig, client: Optional[RestClient]) -> MetricsSource:
    if cfg.kind == "metrics_app_rest":
        assert client is not None
        return MetricsAppRestSource(cfg, client)
    if cfg.kind == "metrics_app_sempy":
        return MetricsAppSempySource(cfg)
    if cfg.kind == "fake":
        return FakeSource()
    raise ValueError(f"metrics_source.kind desconhecido: {cfg.kind}")
