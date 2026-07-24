"""Store em Delta, para quem roda dentro de um notebook Fabric com Lakehouse anexado.

Usa a sessão Spark ambiente. Fora do Fabric, prefira o SqliteStore: o overhead do Spark
para milhares de linhas por ciclo não se paga.
"""

from __future__ import annotations

import datetime as dt
from typing import Dict, List, Optional, Sequence

from ..models import Event, Interval, ItemSnapshot, ItemState


def _spark():
    try:
        from pyspark.sql import SparkSession  # type: ignore
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "storage.kind='delta' exige PySpark — use dentro de um notebook Fabric, "
            "ou troque para storage.kind='sqlite'."
        ) from e
    session = SparkSession.getActiveSession()
    if session is None:
        raise RuntimeError("Nenhuma SparkSession ativa. Anexe um Lakehouse ao notebook.")
    return session


class DeltaStore:
    def __init__(self, prefix: str = "watchdog_") -> None:
        self.prefix = prefix
        self.spark = _spark()

    def _t(self, name: str) -> str:
        return f"{self.prefix}{name}"

    def _exists(self, name: str) -> bool:
        return self.spark.catalog.tableExists(self._t(name))

    def init_schema(self) -> None:
        # Delta cria as tabelas no primeiro append; nada a fazer aqui.
        return

    # -------------------------------------------------------------- snapshots

    def write_snapshots(self, rows: Sequence[ItemSnapshot]) -> None:
        if not rows:
            return
        data = [
            (
                r.ts, r.capacity_key, r.capacity_id, r.item_id, r.item_name, r.item_kind,
                r.workspace_id, r.workspace_name, float(r.cu_seconds_today),
            )
            for r in rows
        ]
        cols = [
            "ts", "capacity_key", "capacity_id", "item_id", "item_name", "item_kind",
            "workspace_id", "workspace_name", "cu_seconds_today",
        ]
        self.spark.createDataFrame(data, cols).write.mode("append").saveAsTable(self._t("snapshots"))

    def previous_snapshots(self, capacity_key: str, before: dt.datetime) -> Dict[str, ItemSnapshot]:
        if not self._exists("snapshots"):
            return {}
        from pyspark.sql import Window  # type: ignore
        from pyspark.sql import functions as F  # type: ignore

        df = self.spark.table(self._t("snapshots")).filter(
            (F.col("capacity_key") == capacity_key) & (F.col("ts") < F.lit(before))
        )
        w = Window.partitionBy("item_id").orderBy(F.desc("ts"))
        latest = df.withColumn("_rn", F.row_number().over(w)).filter(F.col("_rn") == 1)
        return {
            r["item_id"]: ItemSnapshot(
                ts=r["ts"], capacity_key=r["capacity_key"], capacity_id=r["capacity_id"] or "",
                item_id=r["item_id"], item_name=r["item_name"] or "", item_kind=r["item_kind"] or "",
                workspace_id=r["workspace_id"] or "", workspace_name=r["workspace_name"] or "",
                cu_seconds_today=float(r["cu_seconds_today"] or 0.0),
            )
            for r in latest.collect()
        }

    # -------------------------------------------------------------- intervals

    def write_intervals(self, rows: Sequence[Interval]) -> None:
        if not rows:
            return
        data = [
            (
                r.capacity_key, r.item_id, r.window_start, r.window_end, r.item_name, r.item_kind,
                r.workspace_id, r.workspace_name, float(r.cu_seconds),
                r.date.isoformat(), r.hour, r.dow,
            )
            for r in rows
        ]
        cols = [
            "capacity_key", "item_id", "window_start", "window_end", "item_name", "item_kind",
            "workspace_id", "workspace_name", "cu_seconds", "day", "hour", "dow",
        ]
        self.spark.createDataFrame(data, cols).write.mode("append").saveAsTable(self._t("intervals"))

    def load_intervals(
        self, capacity_key: str, since: dt.date, until: dt.date, hours: Optional[Sequence[int]] = None
    ) -> List[Interval]:
        if not self._exists("intervals"):
            return []
        from pyspark.sql import functions as F  # type: ignore

        df = self.spark.table(self._t("intervals")).filter(
            (F.col("capacity_key") == capacity_key)
            & (F.col("day") >= since.isoformat())
            & (F.col("day") < until.isoformat())
        )
        if hours:
            df = df.filter(F.col("hour").isin(list(hours)))
        return [
            Interval(
                capacity_key=r["capacity_key"], item_id=r["item_id"], item_name=r["item_name"] or "",
                item_kind=r["item_kind"] or "", workspace_id=r["workspace_id"] or "",
                workspace_name=r["workspace_name"] or "", window_start=r["window_start"],
                window_end=r["window_end"], cu_seconds=float(r["cu_seconds"] or 0.0),
            )
            for r in df.collect()
        ]

    # -------------------------------------------------------------- events / state

    def write_events(self, rows: Sequence[Event]) -> None:
        if not rows:
            return
        data = [tuple(e.__dict__.values()) for e in rows]
        cols = list(rows[0].__dict__.keys())
        self.spark.createDataFrame(data, cols).write.mode("append").saveAsTable(self._t("events"))

    def load_events(self, since: dt.datetime, capacity_key: Optional[str] = None) -> List[Event]:
        if not self._exists("events"):
            return []
        from pyspark.sql import functions as F  # type: ignore

        df = self.spark.table(self._t("events")).filter(F.col("ts") >= F.lit(since))
        if capacity_key:
            df = df.filter(F.col("capacity_key") == capacity_key)
        return [Event(**r.asDict()) for r in df.orderBy(F.desc("ts")).collect()]

    def load_states(self, capacity_key: str) -> Dict[str, ItemState]:
        if not self._exists("item_state"):
            return {}
        from pyspark.sql import functions as F  # type: ignore

        df = self.spark.table(self._t("item_state")).filter(F.col("capacity_key") == capacity_key)
        return {r["item_id"]: ItemState(**r.asDict()) for r in df.collect()}

    def save_states(self, rows: Sequence[ItemState]) -> None:
        """Sobrescreve o estado da capacidade — é um snapshot pequeno, não um log."""
        if not rows:
            return
        from delta.tables import DeltaTable  # type: ignore

        data = [
            (s.capacity_key, s.item_id, s.streak, s.last_tier, s.last_action_ts, s.last_seen_ts)
            for s in rows
        ]
        cols = ["capacity_key", "item_id", "streak", "last_tier", "last_action_ts", "last_seen_ts"]
        df = self.spark.createDataFrame(data, cols)
        table = self._t("item_state")
        if not self._exists("item_state"):
            df.write.mode("overwrite").saveAsTable(table)
            return
        DeltaTable.forName(self.spark, table).alias("t").merge(
            df.alias("s"), "t.capacity_key = s.capacity_key AND t.item_id = s.item_id"
        ).whenMatchedUpdateAll().whenNotMatchedInsertAll().execute()

    def close(self) -> None:
        return
