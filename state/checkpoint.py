"""Checkpoint 的构建、持久化与恢复计划。

Checkpoint 保存恢复所需的最小信息：完整 Messages、当前批次待执行工具、
执行位置。真正的 Agent Resume 尚未接入 Runner；本模块只提供可靠的数据基础
和保守的恢复决策。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from .journal import ToolPhase
from .messages import RecoveryError, rebuild_messages
from .models import AgentState, RunStatus
from .recovery import RecoveryAction, decide_recovery

RESUMABLE_STATUSES = frozenset({
    RunStatus.RUNNING,
    RunStatus.RECOVERING,
    RunStatus.INTERRUPTED,
    RunStatus.WAITING_APPROVAL,
})

_TERMINAL_TOOL_PHASES = frozenset({
    ToolPhase.SUCCEEDED,
    ToolPhase.FAILED,
    ToolPhase.SKIPPED,
})


@dataclass
class Checkpoint:
    run_id: str
    step: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    pending_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    next_tool_index: int = 0
    checkpoint_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: str | None = None


@dataclass
class BatchInspection:
    """一次批量 Tool Calls 的日志与 Checkpoint 对照结果。"""

    run_id: str
    checkpoint_id: str | None
    next_tool_index: int
    completed: list[dict[str, Any]] = field(default_factory=list)
    not_executed: list[dict[str, Any]] = field(default_factory=list)
    uncertain: list[dict[str, Any]] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        return not self.issues


def _decode_result(record) -> Any:
    if record is None or record.result_json is None:
        return None
    try:
        return json.loads(record.result_json)
    except (TypeError, ValueError):
        return record.result_json


class CheckpointManager:
    def __init__(self, store):
        self.store = store

    def build(
        self,
        run_id: str,
        *,
        messages: list[dict[str, Any]],
        step: int,
        pending_tool_calls: list[dict[str, Any]] | None = None,
        next_tool_index: int = 0,
    ) -> Checkpoint:
        return Checkpoint(
            run_id=run_id,
            step=step,
            messages=list(messages),
            pending_tool_calls=list(pending_tool_calls or []),
            next_tool_index=next_tool_index,
        )

    @staticmethod
    def bind(state: AgentState, checkpoint: Checkpoint) -> None:
        """把 Checkpoint 指针同步到 AgentState。"""
        state.checkpoint_id = checkpoint.checkpoint_id
        state.pending_tool_calls = list(checkpoint.pending_tool_calls)
        state.resume_cursor = checkpoint.next_tool_index

    def save(
        self,
        state: AgentState,
        *,
        messages: list[dict[str, Any]],
        step: int | None = None,
        pending_tool_calls: list[dict[str, Any]] | None = None,
        next_tool_index: int = 0,
        event_type: str = "checkpoint.saved",
    ) -> Checkpoint:
        checkpoint = self.build(
            state.run_id,
            messages=messages,
            step=state.iteration if step is None else step,
            pending_tool_calls=pending_tool_calls,
            next_tool_index=next_tool_index,
        )
        self.bind(state, checkpoint)
        self.store.commit(
            state,
            event_type,
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "step": checkpoint.step,
                "next_tool_index": checkpoint.next_tool_index,
            },
            checkpoint=checkpoint,
        )
        return checkpoint

    def latest(self, run_id: str) -> Checkpoint | None:
        return self.store.latest_checkpoint(run_id)

    def get(self, checkpoint_id: str) -> Checkpoint | None:
        return self.store.get_checkpoint(checkpoint_id)

    def inspect_batch(self, run_id: str) -> BatchInspection:
        """对照 Checkpoint、Messages 与 Tool Journal。

        区分已完成、确定未执行、执行状态不确定的 Tool Call，并报告
        执行位置与日志不一致的情况。
        """
        checkpoint = self.latest(run_id)
        if checkpoint is None:
            return BatchInspection(
                run_id=run_id,
                checkpoint_id=None,
                next_tool_index=0,
                issues=["没有可用的 Checkpoint"],
            )

        records = {
            record.call_id: record
            for record in self.store.load_tools(run_id)
        }
        completed: list[dict[str, Any]] = []
        not_executed: list[dict[str, Any]] = []
        uncertain: list[dict[str, Any]] = []
        issues: list[str] = []
        pending_ids: list[str] = []
        seen_ids: set[str] = set()

        for index, call in enumerate(checkpoint.pending_tool_calls):
            if not isinstance(call, dict):
                issues.append(f"index {index} 的待执行工具格式不正确")
                continue

            call_id = call.get("id")
            function = call.get("function")
            name = (
                function.get("name")
                if isinstance(function, dict)
                else None
            )

            if not isinstance(call_id, str) or not call_id:
                issues.append(f"index {index} 的待执行工具缺少有效 call_id")
                continue
            if call_id in seen_ids:
                issues.append(f"待执行工具 call_id 重复: {call_id}")
                continue

            seen_ids.add(call_id)
            pending_ids.append(call_id)
            record = records.get(call_id)
            if (
                record is not None
                and isinstance(name, str)
                and record.name != name
            ):
                issues.append(
                    f"index {index} 的工具名不一致："
                    f"Checkpoint={name}，Journal={record.name}"
                )
            entry = {
                "index": index,
                "call_id": call_id,
                "name": name,
                "phase": (
                    record.phase.value if record is not None else None
                ),
            }

            if record is None or record.phase in {
                ToolPhase.PREPARED,
                ToolPhase.SKIPPED,
                ToolPhase.MANUAL_RETRY,
            }:
                not_executed.append(entry)
            elif record.phase in {
                ToolPhase.EXECUTING,
                ToolPhase.UNKNOWN,
            }:
                uncertain.append(entry)
            else:
                completed.append(entry)

            terminal = {
                ToolPhase.SUCCEEDED,
                ToolPhase.FAILED,
                ToolPhase.SKIPPED,
                ToolPhase.MANUAL_RETRY,
            }
            if index < checkpoint.next_tool_index:
                if record is None:
                    issues.append(
                        f"index {index} 已越过执行位置，"
                        f"但缺少工具日志: {call_id}"
                    )
                elif record.phase not in terminal:
                    issues.append(
                        f"index {index} 已越过执行位置，"
                        f"但工具状态仍为 {record.phase.value}: {call_id}"
                    )
            elif index > checkpoint.next_tool_index and record is not None:
                issues.append(
                    f"index {index} 尚未到达执行位置，"
                    f"但已存在工具日志: {call_id}"
                )

        if not 0 <= checkpoint.next_tool_index <= len(
            checkpoint.pending_tool_calls
        ):
            issues.append(
                f"执行位置越界: {checkpoint.next_tool_index}"
            )

        if checkpoint.pending_tool_calls:
            message_ids: list[str] | None = None
            for message in reversed(checkpoint.messages):
                if (
                    isinstance(message, dict)
                    and message.get("role") == "assistant"
                    and message.get("tool_calls")
                ):
                    message_ids = [
                        call.get("id")
                        for call in message["tool_calls"]
                        if isinstance(call, dict)
                    ]
                    break

            if message_ids is None:
                issues.append(
                    "Checkpoint 含待执行工具，"
                    "但 Messages 中没有 assistant tool_calls"
                )
            elif message_ids != pending_ids:
                issues.append(
                    "Checkpoint 待执行工具与 Messages 中的 "
                    "tool_calls 不一致"
                )

        return BatchInspection(
            run_id=run_id,
            checkpoint_id=checkpoint.checkpoint_id,
            next_tool_index=checkpoint.next_tool_index,
            completed=completed,
            not_executed=not_executed,
            uncertain=uncertain,
            issues=issues,
        )

    def prepare_messages(self, run_id: str) -> list[dict[str, Any]]:
        """恢复前重建完整 Messages；无法安全重建时抛 RecoveryError。"""
        checkpoint = self.latest(run_id)
        if checkpoint is None:
            raise RecoveryError(f"Run {run_id} 没有可用的 Checkpoint")

        inspection = self.inspect_batch(run_id)
        if inspection.issues:
            raise RecoveryError(
                "Checkpoint 与工具日志不一致："
                + "；".join(inspection.issues)
            )

        records = {
            record.call_id: record
            for record in self.store.load_tools(run_id)
        }
        return rebuild_messages(checkpoint.messages, records)

    def can_resume(self, run_id: str) -> bool:
        """状态与 Checkpoint 是否具备恢复的基础条件。"""
        state = self.store.load(run_id)
        if state is None or state.status not in RESUMABLE_STATUSES:
            return False
        return self.latest(run_id) is not None

    def resume_plan(self, run_id: str) -> list[dict[str, Any]]:
        """列出尚未安全落地的工具调用及建议动作。"""
        checkpoint = self.latest(run_id)
        if checkpoint is None:
            return []

        records = {
            record.call_id: record
            for record in self.store.load_tools(run_id)
        }
        plan: list[dict[str, Any]] = []

        for index, call in enumerate(checkpoint.pending_tool_calls):
            call_id = call.get("id")
            record = records.get(call_id)

            # 执行位置之前的工具若已有明确结果，不需要再次处理。
            if (
                index < checkpoint.next_tool_index
                and record is not None
                and record.phase in _TERMINAL_TOOL_PHASES
            ):
                continue

            action = decide_recovery(record)
            function = call.get("function") or {}
            plan.append({
                "index": index,
                "call_id": call_id,
                "name": function.get("name"),
                "phase": record.phase.value if record is not None else None,
                "action": action.value,
                "result": _decode_result(record),
            })

        return plan

    def can_auto_resume(self, run_id: str) -> bool:
        """没有需要人工确认的写工具时，才允许自动恢复。"""
        if not self.can_resume(run_id):
            return False
        return all(
            item["action"] != RecoveryAction.REQUIRE_APPROVAL.value
            for item in self.resume_plan(run_id)
        )
