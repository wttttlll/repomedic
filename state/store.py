"""SQLite 持久化：运行状态、事件、工具执行日志与 Checkpoint。

所有写操作都在显式事务中完成；agent_runs 使用 version 做乐观并发控制。
每次操作单独建立连接，因此进程重启后可直接从磁盘读取。
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Iterator

from .checkpoint import Checkpoint
from .journal import ToolPhase, ToolRecord
from .models import AgentState, RunStatus

# 单个工具输出可能非常大（例如完整测试日志），限制入库体积。
MAX_RESULT_CHARS = 100_000


class StateConflict(Exception):
    """乐观版本号冲突，说明同一 Run 被并发修改。"""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _encode_result(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else _json(value)
    truncated = len(text) > MAX_RESULT_CHARS
    if truncated:
        text = text[:MAX_RESULT_CHARS]
    return _json({"output": text, "truncated": truncated})


class SQLiteStateStore:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # ------------------------------------------------------------------ 连接
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(
            self.path,
            timeout=10,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                conn.execute("COMMIT")

    def _initialize(self) -> None:
        with self._transaction() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS agent_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_agent_events_run
                ON agent_events(run_id, id)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step INTEGER NOT NULL,
                    next_tool_index INTEGER NOT NULL DEFAULT 0,
                    messages_json TEXT NOT NULL,
                    pending_tool_calls_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_checkpoints_run
                ON checkpoints(run_id, created_at)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tool_executions (
                    run_id TEXT NOT NULL,
                    call_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    arguments_json TEXT NOT NULL DEFAULT '{}',
                    phase TEXT NOT NULL,
                    result_json TEXT,
                    idempotency_key TEXT,
                    updated_at TEXT NOT NULL
                        DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (run_id, call_id)
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_tool_executions_run
                ON tool_executions(run_id, updated_at)
            """)

    # ------------------------------------------------------------------ 状态
    @staticmethod
    def _serialize_state(state: AgentState) -> str:
        data = asdict(state)
        data["status"] = state.status.value
        return _json(data)

    @staticmethod
    def _row_to_state(row: sqlite3.Row) -> AgentState:
        data = json.loads(row["state_json"])
        data["status"] = RunStatus(data["status"])
        known = {item.name for item in fields(AgentState)}
        return AgentState(**{
            key: value for key, value in data.items() if key in known
        })

    def create(self, state: AgentState) -> None:
        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO agent_runs
                (run_id, session_id, status,
                 version, state_json)
                VALUES (?, ?, ?, ?, ?)
            """, (
                state.run_id,
                state.session_id,
                state.status.value,
                state.version,
                self._serialize_state(state),
            ))

    def load(self, run_id: str) -> AgentState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM agent_runs "
                "WHERE run_id = ?",
                (run_id,)
            ).fetchone()

        if row is None:
            return None
        return self._row_to_state(row)

    def list_runs(
        self,
        session_id: str | None = None,
        status: RunStatus | None = None,
        limit: int = 50,
    ) -> list[AgentState]:
        clauses: list[str] = []
        params: list[Any] = []

        if session_id is not None:
            clauses.append("session_id = ?")
            params.append(session_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status.value)

        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT state_json FROM agent_runs "
                f"{where}ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                params,
            ).fetchall()

        return [self._row_to_state(row) for row in rows]

    def commit(
        self,
        state: AgentState,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        checkpoint: Checkpoint | None = None,
        tool: ToolRecord | None = None,
        tool_phase: tuple[str, ToolPhase, str] | None = None,
        tool_result: Any = None,
        tool_overwrite_result: bool = False,
    ) -> None:
        """原子提交状态、事件、Checkpoint 和工具日志。"""
        next_version = state.version + 1
        data = asdict(state)
        data["status"] = state.status.value
        data["version"] = next_version

        with self._transaction() as conn:
            cursor = conn.execute("""
                UPDATE agent_runs
                SET status = ?,
                    version = ?,
                    state_json = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = ? AND version = ?
            """, (
                state.status.value,
                next_version,
                _json(data),
                state.run_id,
                state.version,
            ))

            if cursor.rowcount != 1:
                raise StateConflict("State version conflict")

            conn.execute("""
                INSERT INTO agent_events
                (run_id, event_type, payload_json)
                VALUES (?, ?, ?)
            """, (
                state.run_id,
                event_type,
                _json(payload or {}),
            ))

            if checkpoint is not None:
                conn.execute("""
                    INSERT INTO checkpoints
                    (checkpoint_id, run_id, step,
                     next_tool_index, messages_json,
                     pending_tool_calls_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    checkpoint.checkpoint_id,
                    checkpoint.run_id,
                    checkpoint.step,
                    checkpoint.next_tool_index,
                    _json(checkpoint.messages),
                    _json(checkpoint.pending_tool_calls),
                ))

            if state.checkpoint_id:
                conn.execute("""
                    UPDATE checkpoints
                    SET next_tool_index = ?
                    WHERE checkpoint_id = ? AND run_id = ?
                """, (
                    state.resume_cursor,
                    state.checkpoint_id,
                    state.run_id,
                ))

            if tool is not None:
                conn.execute("""
                    INSERT INTO tool_executions
                    (run_id, call_id, name, arguments_json,
                     phase, result_json, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, call_id) DO UPDATE SET
                        name = excluded.name,
                        arguments_json = excluded.arguments_json,
                        phase = excluded.phase,
                        result_json = excluded.result_json,
                        idempotency_key = excluded.idempotency_key,
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    tool.run_id,
                    tool.call_id,
                    tool.name,
                    tool.arguments_json,
                    tool.phase.value,
                    tool.result_json,
                    tool.idempotency_key,
                ))
            elif tool_phase is not None:
                call_id, phase, name = tool_phase
                result_update = (
                    "excluded.result_json"
                    if tool_overwrite_result
                    else "COALESCE(excluded.result_json, "
                         "tool_executions.result_json)"
                )
                conn.execute(f"""
                    INSERT INTO tool_executions
                    (run_id, call_id, name, phase, result_json)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(run_id, call_id) DO UPDATE SET
                        name = CASE
                            WHEN excluded.name = ''
                            THEN tool_executions.name
                            ELSE excluded.name
                        END,
                        phase = excluded.phase,
                        result_json = {result_update},
                        updated_at = CURRENT_TIMESTAMP
                """, (
                    state.run_id,
                    call_id,
                    name,
                    phase.value,
                    _encode_result(tool_result),
                ))

        # 只有事务真正提交后才推进内存版本号。
        state.version = next_version

    # ---------------------------------------------------------- Checkpoint
    @staticmethod
    def _row_to_checkpoint(row: sqlite3.Row) -> Checkpoint:
        return Checkpoint(
            checkpoint_id=row["checkpoint_id"],
            run_id=row["run_id"],
            step=row["step"],
            next_tool_index=row["next_tool_index"],
            messages=json.loads(row["messages_json"]),
            pending_tool_calls=json.loads(
                row["pending_tool_calls_json"]
            ),
            created_at=row["created_at"],
        )

    def latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints "
                "WHERE run_id = ? ORDER BY rowid DESC LIMIT 1",
                (run_id,),
            ).fetchone()

        return self._row_to_checkpoint(row) if row is not None else None

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?",
                (checkpoint_id,),
            ).fetchone()

        return self._row_to_checkpoint(row) if row is not None else None

    def list_checkpoints(self, run_id: str) -> list[Checkpoint]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM checkpoints "
                "WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()

        return [self._row_to_checkpoint(row) for row in rows]

    def prune_checkpoints(
        self,
        *,
        keep_per_run: int = 5,
        run_id: str | None = None,
    ) -> int:
        """只清理已完成/失败 Run 的旧 Checkpoint。

        未完成 Run（RUNNING / INTERRUPTED / WAITING_APPROVAL 等）
        的恢复数据永远不会被删除。
        """
        keep = max(1, int(keep_per_run))
        terminal_status = {
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
        }
        deleted = 0

        with self._transaction() as conn:
            if run_id is not None:
                rows = conn.execute(
                    "SELECT run_id, status FROM agent_runs "
                    "WHERE run_id = ?",
                    (run_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT run_id, status FROM agent_runs "
                    "WHERE status IN (?, ?)",
                    (
                        RunStatus.COMPLETED.value,
                        RunStatus.FAILED.value,
                    ),
                ).fetchall()

            for row in rows:
                if row["status"] not in terminal_status:
                    continue
                checkpoints = conn.execute(
                    "SELECT checkpoint_id FROM checkpoints "
                    "WHERE run_id = ? ORDER BY rowid DESC",
                    (row["run_id"],),
                ).fetchall()
                stale = [
                    item["checkpoint_id"]
                    for item in checkpoints[keep:]
                ]
                if not stale:
                    continue
                conn.executemany(
                    "DELETE FROM checkpoints WHERE checkpoint_id = ?",
                    [(checkpoint_id,) for checkpoint_id in stale],
                )
                deleted += len(stale)

        return deleted

    # -------------------------------------------------------------- 工具日志
    def load_tools(self, run_id: str) -> list[ToolRecord]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tool_executions "
                "WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()

        return [
            ToolRecord(
                run_id=row["run_id"],
                call_id=row["call_id"],
                name=row["name"],
                arguments_json=row["arguments_json"],
                phase=ToolPhase(row["phase"]),
                result_json=row["result_json"],
                idempotency_key=row["idempotency_key"],
            )
            for row in rows
        ]

    def get_tool(self, run_id: str, call_id: str) -> ToolRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM tool_executions "
                "WHERE run_id = ? AND call_id = ?",
                (run_id, call_id),
            ).fetchone()

        if row is None:
            return None
        return ToolRecord(
            run_id=row["run_id"],
            call_id=row["call_id"],
            name=row["name"],
            arguments_json=row["arguments_json"],
            phase=ToolPhase(row["phase"]),
            result_json=row["result_json"],
            idempotency_key=row["idempotency_key"],
        )

    # ------------------------------------------------------------------ 事件
    def latest_checkpoint_event(
        self, run_id: str
    ) -> dict[str, Any] | None:
        """最近一次与 Checkpoint 对应的阶段事件，忽略失败/恢复控制事件。"""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_type, payload_json, created_at "
                "FROM agent_events WHERE run_id = ? AND event_type IN "
                "('run.started', 'run.iteration', 'tools.planned', "
                "'iteration.completed', 'checkpoint.saved') "
                "ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()

        if row is None:
            return None
        return {
            "event_type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def latest_event(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT event_type, payload_json, created_at "
                "FROM agent_events WHERE run_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (run_id,),
            ).fetchone()

        if row is None:
            return None
        return {
            "event_type": row["event_type"],
            "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"],
        }

    def list_events(
        self,
        run_id: str,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_type, payload_json, created_at "
                "FROM agent_events WHERE run_id = ? "
                "ORDER BY id LIMIT ?",
                (run_id, limit),
            ).fetchall()

        return [
            {
                "event_type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]
