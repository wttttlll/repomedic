"""Agent 运行状态的数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from uuid import uuid4


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.COMPLETED, RunStatus.FAILED}


class ToolStatus(str, Enum):
    STARTED = "started"
    EXECUTED = "executed"
    SKIPPED = "skipped"
    RAISED = "raised"


@dataclass
class AgentState:
    session_id: str
    task: str
    run_id: str = field(default_factory=lambda: str(uuid4()))
    status: RunStatus = RunStatus.PENDING
    iteration: int = 0
    current_tool_call_id: str | None = None
    current_tool_name: str | None = None
    error: str | None = None
    result: str | None = None
    version: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    checkpoint_id: str | None = None
    resume_cursor: int = 0
    pending_tool_call_id: str | None = None
    # 当前批次中模型请求、但尚未确认结果的工具调用。
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
