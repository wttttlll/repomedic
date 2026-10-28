"""工具执行日志：记录每个 Tool Call 的副作用阶段。"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ToolPhase(str, Enum):
    # 已登记但尚未真正执行，可安全重试。
    PREPARED = "prepared"
    # 正在执行；进程若在此阶段退出，无法确定是否产生副作用。
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    # 因修复次数 / 环境异常被跳过，未执行。
    SKIPPED = "skipped"
    # 人工确认未执行，允许重新执行。
    MANUAL_RETRY = "manual_retry"
    UNKNOWN = "unknown"

    @classmethod
    def from_outcome(cls, outcome: str) -> "ToolPhase":
        return {
            "executed": cls.SUCCEEDED,
            "skipped": cls.SKIPPED,
            "raised": cls.FAILED,
        }.get(outcome, cls.FAILED)


@dataclass
class ToolRecord:
    run_id: str
    call_id: str
    name: str
    arguments_json: str
    phase: ToolPhase
    result_json: str | None = None
    idempotency_key: str | None = None
