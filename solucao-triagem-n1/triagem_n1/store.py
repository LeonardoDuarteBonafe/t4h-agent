"""Persistent, run-scoped state and audit history for the triage application.

Every operation owns its SQLite connection.  Cases, traces, decisions and remote
operation journals are isolated by run; sourced lessons deliberately survive a
new run.  Approval policy belongs to the caller, not this audit layer.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def _dump(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, default=_json_default,
        allow_nan=False,
    )


class Store:
    """Small SQLite repository with one transaction per public write method."""

    def __init__(self, path: Path | str, run_id: str) -> None:
        if not str(run_id).strip():
            raise ValueError("run_id must not be empty")
        if str(path) == ":memory:":
            raise ValueError("Use a file path so state survives connection closure")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = str(run_id)
        with self._connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS cases (
                    run_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, id)
                );
                CREATE TABLE IF NOT EXISTS traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    agent TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS traces_case
                    ON traces(run_id, case_id, id);
                CREATE TABLE IF NOT EXISTS operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    operation_key TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    tool TEXT NOT NULL,
                    args_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(run_id, operation_key)
                );
                CREATE INDEX IF NOT EXISTS operations_case
                    ON operations(run_id, case_id, id);
                CREATE TABLE IF NOT EXISTS decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    case_id TEXT NOT NULL,
                    version_json TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    note TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS decisions_case
                    ON decisions(run_id, case_id, id);
                CREATE TABLE IF NOT EXISTS lessons (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    text TEXT NOT NULL,
                    source_json TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    seen_count INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(text, source_json)
                );
                """
            )

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(str(self.path), timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def put_case(self, data: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(data, dict) or not str(data.get("id") or "").strip():
            raise ValueError("Case data must include a non-empty id")
        case_id = str(data["id"])
        stamp = _now()
        with self._connection(write=True) as db:
            row = db.execute(
                "SELECT data_json, created_at FROM cases WHERE run_id=? AND id=?",
                (self.run_id, case_id),
            ).fetchone()
            merged = json.loads(row["data_json"]) if row else {}
            merged.update(data)
            merged["id"] = case_id
            merged["run_id"] = self.run_id
            merged["created_at"] = row["created_at"] if row else stamp
            merged["updated_at"] = stamp
            encoded = _dump(merged)
            db.execute(
                """INSERT INTO cases(run_id,id,data_json,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(run_id,id) DO UPDATE SET
                   data_json=excluded.data_json, updated_at=excluded.updated_at""",
                (self.run_id, case_id, encoded, merged["created_at"], stamp),
            )
        return json.loads(encoded)

    def get_case(self, case_id: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT data_json FROM cases WHERE run_id=? AND id=?",
                (self.run_id, str(case_id)),
            ).fetchone()
        return json.loads(row["data_json"]) if row else None

    def list_cases(self) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT data_json FROM cases WHERE run_id=? ORDER BY updated_at DESC, id",
                (self.run_id,),
            ).fetchall()
        return [json.loads(row["data_json"]) for row in rows]

    def trace(self, case_id: str, agent: str, stage: str, data: Any) -> dict[str, Any]:
        stamp, encoded = _now(), _dump(data)
        with self._connection(write=True) as db:
            cursor = db.execute(
                """INSERT INTO traces(run_id,case_id,agent,stage,data_json,timestamp)
                   VALUES(?,?,?,?,?,?)""",
                (self.run_id, str(case_id), str(agent), str(stage), encoded, stamp),
            )
            trace_id = cursor.lastrowid
        return {"id": trace_id, "run_id": self.run_id, "case_id": str(case_id),
                "agent": str(agent), "stage": str(stage),
                "data": json.loads(encoded), "timestamp": stamp}

    def traces(self, case_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM traces WHERE run_id=? AND case_id=? ORDER BY id",
                (self.run_id, str(case_id)),
            ).fetchall()
        return [self._decoded(row, data="data_json") for row in rows]

    def operation(
        self, key: str, case_id: str, tool: str, args: Any,
        status: str = "intent", result: Any = None,
    ) -> dict[str, Any]:
        if not str(key).strip():
            raise ValueError("Operation key must not be empty")
        stamp, args_json = _now(), _dump(args)
        result_json = _dump(result) if result is not None else None
        with self._connection(write=True) as db:
            previous = db.execute(
                "SELECT * FROM operations WHERE run_id=? AND operation_key=?",
                (self.run_id, str(key)),
            ).fetchone()
            if previous:
                if (previous["case_id"] != str(case_id)
                        or previous["tool"] != str(tool)
                        or previous["args_json"] != args_json):
                    raise ValueError("Operation key already belongs to another intent")
                # Reopening an existing intent must not erase a confirmed or
                # ambiguous outcome. An explicit transition can still update it.
                if status != "intent":
                    db.execute(
                        """UPDATE operations SET status=?, result_json=?, updated_at=?
                           WHERE run_id=? AND operation_key=?""",
                        (str(status), result_json if result is not None else previous["result_json"],
                         stamp, self.run_id, str(key)),
                    )
            else:
                db.execute(
                    """INSERT INTO operations(run_id,operation_key,case_id,tool,args_json,
                       status,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (self.run_id, str(key), str(case_id), str(tool), args_json,
                     str(status), result_json, stamp, stamp),
                )
            row = db.execute(
                "SELECT * FROM operations WHERE run_id=? AND operation_key=?",
                (self.run_id, str(key)),
            ).fetchone()
        return self._operation_row(row)

    def get_operation(self, key: str) -> dict[str, Any] | None:
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM operations WHERE run_id=? AND operation_key=?",
                (self.run_id, str(key)),
            ).fetchone()
        return self._operation_row(row) if row else None

    def list_operations(self, case_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM operations WHERE run_id=?"
        params: tuple[Any, ...] = (self.run_id,)
        if case_id is not None:
            query += " AND case_id=?"
            params += (str(case_id),)
        with self._connection() as db:
            rows = db.execute(query + " ORDER BY id", params).fetchall()
        return [self._operation_row(row) for row in rows]

    def decision(
        self, case_id: str, version: Any, actor: str, decision: str, note: str = "",
    ) -> dict[str, Any]:
        stamp = _now()
        encoded_version = _dump(version)
        with self._connection(write=True) as db:
            cursor = db.execute(
                """INSERT INTO decisions(run_id,case_id,version_json,actor,decision,note,timestamp)
                   VALUES(?,?,?,?,?,?,?)""",
                (self.run_id, str(case_id), encoded_version, str(actor),
                 str(decision), str(note), stamp),
            )
            decision_id = cursor.lastrowid
        return {"id": decision_id, "run_id": self.run_id, "case_id": str(case_id),
                "version": json.loads(encoded_version), "actor": str(actor),
                "decision": str(decision), "note": str(note), "timestamp": stamp}

    def decisions(self, case_id: str) -> list[dict[str, Any]]:
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM decisions WHERE run_id=? AND case_id=? ORDER BY id",
                (self.run_id, str(case_id)),
            ).fetchall()
        return [self._decoded(row, version="version_json") for row in rows]

    def add_lesson(self, text: str, source: Any) -> dict[str, Any]:
        if not str(text).strip() or not source:
            raise ValueError("A lesson requires text and a source")
        stamp, source_json = _now(), _dump(source)
        with self._connection(write=True) as db:
            db.execute(
                """INSERT INTO lessons(text,source_json,run_id,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(text,source_json) DO UPDATE SET
                   updated_at=excluded.updated_at, seen_count=lessons.seen_count+1""",
                (str(text), source_json, self.run_id, stamp, stamp),
            )
            row = db.execute(
                "SELECT * FROM lessons WHERE text=? AND source_json=?",
                (str(text), source_json),
            ).fetchone()
        return self._decoded(row, source="source_json")

    def lessons(self, limit: int = 10) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM lessons ORDER BY updated_at DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._decoded(row, source="source_json") for row in rows]

    @staticmethod
    def _decoded(row: sqlite3.Row, **fields: str) -> dict[str, Any]:
        result = dict(row)
        for name, column in fields.items():
            value = result.pop(column)
            result[name] = json.loads(value) if value is not None else None
        return result

    @classmethod
    def _operation_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        result = cls._decoded(row, args="args_json", result="result_json")
        result["key"] = result.pop("operation_key")
        return result
