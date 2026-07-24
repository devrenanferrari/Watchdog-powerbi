"""Store padrão: SQLite. Zero infraestrutura, suficiente para dezenas de capacidades.

Aponte `storage.path` para um volume persistente (Azure Files, PVC, OneLake shortcut).
Se o arquivo sumir, o watchdog perde a baseline e volta a observar até reacumular histórico.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from ..models import Event, Interval, ItemSnapshot, ItemState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    ts TEXT NOT NULL, capacity_key TEXT NOT NULL, capacity_id TEXT,
    item_id TEXT NOT NULL, item_name TEXT, item_kind TEXT,
    workspace_id TEXT, workspace_name TEXT, cu_seconds_today REAL,
    PRIMARY KEY (capacity_key, item_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_snapshots_cap_ts ON snapshots(capacity_key, ts DESC);

CREATE TABLE IF NOT EXISTS intervals (
    capacity_key TEXT NOT NULL, item_id TEXT NOT NULL,
    window_start TEXT NOT NULL, window_end TEXT NOT NULL,
    item_name TEXT, item_kind TEXT, workspace_id TEXT, workspace_name TEXT,
    cu_seconds REAL, day TEXT, hour INTEGER, dow INTEGER,
    PRIMARY KEY (capacity_key, item_id, window_end)
);
CREATE INDEX IF NOT EXISTS ix_intervals_lookup ON intervals(capacity_key, day, hour);

CREATE TABLE IF NOT EXISTS events (
    ts TEXT NOT NULL, run_id TEXT, capacity_key TEXT, mode TEXT,
    item_id TEXT, item_name TEXT, item_kind TEXT, workspace_id TEXT,
    tier TEXT, effective_tier TEXT, ratio REAL, cu_seconds REAL,
    baseline_cu_seconds REAL, baseline_days INTEGER, streak INTEGER,
    suppressions TEXT, actions TEXT, detail TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts DESC);

CREATE TABLE IF NOT EXISTS item_state (
    capacity_key TEXT NOT NULL, item_id TEXT NOT NULL,
    streak INTEGER DEFAULT 0, last_tier TEXT, last_action_ts TEXT, last_seen_ts TEXT,
    PRIMARY KEY (capacity_key, item_id)
);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

_ISO = "%Y-%m-%dT%H:%M:%S"


def _dumps(ts: Optional[dt.datetime]) -> Optional[str]:
    return ts.strftime(_ISO) if ts else None


def _loads(raw: Optional[str]) -> Optional[dt.datetime]:
    return dt.datetime.strptime(raw, _ISO) if raw else None


class SqliteStore:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    # ------------------------------------------------------------- snapshots

    def write_snapshots(self, rows: Sequence[ItemSnapshot]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
            [
                (
                    _dumps(r.ts), r.capacity_key, r.capacity_id, r.item_id, r.item_name,
                    r.item_kind, r.workspace_id, r.workspace_name, r.cu_seconds_today,
                )
                for r in rows
            ],
        )
        self.conn.commit()

    def previous_snapshots(self, capacity_key: str, before: dt.datetime) -> Dict[str, ItemSnapshot]:
        """Snapshot mais recente de cada item anterior a `before`."""
        cur = self.conn.execute(
            """
            SELECT s.* FROM snapshots s
            JOIN (
                SELECT item_id, MAX(ts) AS mts FROM snapshots
                WHERE capacity_key = ? AND ts < ? GROUP BY item_id
            ) m ON m.item_id = s.item_id AND m.mts = s.ts
            WHERE s.capacity_key = ?
            """,
            (capacity_key, _dumps(before), capacity_key),
        )
        return {
            r["item_id"]: ItemSnapshot(
                ts=_loads(r["ts"]),
                capacity_key=r["capacity_key"],
                capacity_id=r["capacity_id"] or "",
                item_id=r["item_id"],
                item_name=r["item_name"] or "",
                item_kind=r["item_kind"] or "",
                workspace_id=r["workspace_id"] or "",
                workspace_name=r["workspace_name"] or "",
                cu_seconds_today=r["cu_seconds_today"] or 0.0,
            )
            for r in cur.fetchall()
        }

    # ------------------------------------------------------------- intervals

    def write_intervals(self, rows: Sequence[Interval]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO intervals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    r.capacity_key, r.item_id, _dumps(r.window_start), _dumps(r.window_end),
                    r.item_name, r.item_kind, r.workspace_id, r.workspace_name,
                    r.cu_seconds, r.date.isoformat(), r.hour, r.dow,
                )
                for r in rows
            ],
        )
        self.conn.commit()

    def load_intervals(
        self,
        capacity_key: str,
        since: dt.date,
        until: dt.date,
        hours: Optional[Sequence[int]] = None,
    ) -> List[Interval]:
        sql = "SELECT * FROM intervals WHERE capacity_key = ? AND day >= ? AND day < ?"
        params: list = [capacity_key, since.isoformat(), until.isoformat()]
        if hours:
            sql += f" AND hour IN ({','.join('?' * len(hours))})"
            params += list(hours)
        cur = self.conn.execute(sql, params)
        return [
            Interval(
                capacity_key=r["capacity_key"],
                item_id=r["item_id"],
                item_name=r["item_name"] or "",
                item_kind=r["item_kind"] or "",
                workspace_id=r["workspace_id"] or "",
                workspace_name=r["workspace_name"] or "",
                window_start=_loads(r["window_start"]),
                window_end=_loads(r["window_end"]),
                cu_seconds=r["cu_seconds"] or 0.0,
            )
            for r in cur.fetchall()
        ]

    # ------------------------------------------------------------- events

    def write_events(self, rows: Sequence[Event]) -> None:
        self.conn.executemany(
            "INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    _dumps(e.ts), e.run_id, e.capacity_key, e.mode, e.item_id, e.item_name,
                    e.item_kind, e.workspace_id, e.tier, e.effective_tier, e.ratio,
                    e.cu_seconds, e.baseline_cu_seconds, e.baseline_days, e.streak,
                    e.suppressions, e.actions, e.detail,
                )
                for e in rows
            ],
        )
        self.conn.commit()

    def load_events(self, since: dt.datetime, capacity_key: Optional[str] = None) -> List[Event]:
        sql = "SELECT * FROM events WHERE ts >= ?"
        params: list = [_dumps(since)]
        if capacity_key:
            sql += " AND capacity_key = ?"
            params.append(capacity_key)
        sql += " ORDER BY ts DESC"
        cur = self.conn.execute(sql, params)
        return [
            Event(
                ts=_loads(r["ts"]), run_id=r["run_id"], capacity_key=r["capacity_key"],
                mode=r["mode"], item_id=r["item_id"], item_name=r["item_name"],
                item_kind=r["item_kind"], workspace_id=r["workspace_id"], tier=r["tier"],
                effective_tier=r["effective_tier"], ratio=r["ratio"], cu_seconds=r["cu_seconds"],
                baseline_cu_seconds=r["baseline_cu_seconds"], baseline_days=r["baseline_days"],
                streak=r["streak"], suppressions=r["suppressions"] or "",
                actions=r["actions"] or "", detail=r["detail"] or "",
            )
            for r in cur.fetchall()
        ]

    # ------------------------------------------------------------- state

    def load_states(self, capacity_key: str) -> Dict[str, ItemState]:
        cur = self.conn.execute("SELECT * FROM item_state WHERE capacity_key = ?", (capacity_key,))
        return {
            r["item_id"]: ItemState(
                capacity_key=r["capacity_key"],
                item_id=r["item_id"],
                streak=r["streak"] or 0,
                last_tier=r["last_tier"] or "none",
                last_action_ts=_loads(r["last_action_ts"]),
                last_seen_ts=_loads(r["last_seen_ts"]),
            )
            for r in cur.fetchall()
        }

    def save_states(self, rows: Sequence[ItemState]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO item_state VALUES (?,?,?,?,?,?)",
            [
                (
                    s.capacity_key, s.item_id, s.streak, s.last_tier,
                    _dumps(s.last_action_ts), _dumps(s.last_seen_ts),
                )
                for s in rows
            ],
        )
        self.conn.commit()

    # ------------------------------------------------------------- meta

    def set_meta(self, key: str, value: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO meta VALUES (?,?)", (key, json.dumps(value, default=str))
        )
        self.conn.commit()

    def get_meta(self, key: str) -> Optional[dict]:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else None

    def prune(self, older_than: dt.datetime) -> int:
        """Remove snapshots antigos. Intervalos e eventos ficam — são o histórico auditável."""
        cur = self.conn.execute("DELETE FROM snapshots WHERE ts < ?", (_dumps(older_than),))
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()
