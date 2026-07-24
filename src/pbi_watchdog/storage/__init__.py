"""Persistência de snapshots, intervalos, eventos e estado por item."""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Protocol, Sequence

from ..config import DeltaStorage, StorageConfig
from ..models import Event, Interval, ItemSnapshot, ItemState


class StateStore(Protocol):
    def init_schema(self) -> None: ...
    def write_snapshots(self, rows: Sequence[ItemSnapshot]) -> None: ...
    def previous_snapshots(self, capacity_key: str, before: dt.datetime) -> Dict[str, ItemSnapshot]: ...
    def write_intervals(self, rows: Sequence[Interval]) -> None: ...
    def load_intervals(
        self, capacity_key: str, since: dt.date, until: dt.date, hours: Optional[Sequence[int]] = None
    ) -> List[Interval]: ...
    def write_events(self, rows: Sequence[Event]) -> None: ...
    def load_events(self, since: dt.datetime, capacity_key: Optional[str] = None) -> List[Event]: ...
    def load_states(self, capacity_key: str) -> Dict[str, ItemState]: ...
    def save_states(self, rows: Sequence[ItemState]) -> None: ...
    def close(self) -> None: ...


def build_store(cfg: StorageConfig) -> StateStore:
    if isinstance(cfg, DeltaStorage):
        from .delta_store import DeltaStore

        return DeltaStore(cfg.table_prefix)

    from .sqlite_store import SqliteStore

    return SqliteStore(cfg.path)
