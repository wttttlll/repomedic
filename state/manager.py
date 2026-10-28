"""运行状态的生命周期管理。

Runner 只通过本类更新状态；底层持久化、Checkpoint 与工具日志都由
SQLiteStateStore / CheckpointManager 完成。
"""
from __future__ import annotations

from .approval import (
    ApprovalError,
    find_pending_call,
    parse_tool_call,
    validate_executed,
    validate_not_executed,
)
from .checkpoint import RESUMABLE_STATUSES, CheckpointManager
from .journal import ToolPhase, ToolRecord
from .models import AgentState, RunStatus

MANUAL_REVIEW_STATUSES = frozenset({
    RunStatus.WAITING_APPROVAL,
    RunStatus.INTERRUPTED,
})


class StateManager:
    def __init__(self, store, checkpoints: CheckpointManager | None = None):
        self.store = store
        self.checkpoints = checkpoints or CheckpointManager(store)

    def create(self, session_id: str, task: str) -> AgentState:
        state = AgentState(
            session_id=session_id,
            task=task,
        )
        self.store.create(state)
        return state

    def load(self, run_id: str) -> AgentState | None:
        return self.store.load(run_id)

    # ------------------------------------------------------------ 生命周期
    def start(self, state: AgentState) -> None:
        if state.status != RunStatus.PENDING:
            raise ValueError("Run is not pending")

        state.status = RunStatus.RUNNING
        self.store.commit(state, "run.started")

    def resume(self, state: AgentState) -> None:
        """把可恢复状态切回 RUNNING；不创建新的 run_id。"""
        if state.status not in RESUMABLE_STATUSES:
            raise ValueError(
                f"Run status {state.status.value} is not resumable"
            )

        state.status = RunStatus.RUNNING
        state.error = None
        state.current_tool_call_id = None
        state.current_tool_name = None
        self.store.commit(state, "run.resumed")

    def wait_for_approval(self, state: AgentState, reason: str) -> None:
        """暂停自动恢复，等待人工确认副作用状态。"""
        state.status = RunStatus.WAITING_APPROVAL
        state.error = str(reason)
        self.store.commit(
            state,
            "run.waiting_approval",
            {"reason": str(reason)},
        )

    def _manual_target(self, state: AgentState, call_id: str):
        if state.status not in MANUAL_REVIEW_STATUSES:
            raise ApprovalError(
                "只有 WAITING_APPROVAL/INTERRUPTED Run "
                "可以人工处理"
            )

        try:
            checkpoint = self.checkpoints.latest(state.run_id)
        except (ValueError, KeyError, TypeError) as exc:
            raise ApprovalError(
                f"Checkpoint 数据损坏：{exc}"
            ) from exc
        if checkpoint is None:
            raise ApprovalError("没有可用的 Checkpoint")

        call = find_pending_call(checkpoint, call_id)
        try:
            record = self.store.get_tool(
                state.run_id, call_id
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise ApprovalError(
                f"Tool Journal 数据损坏：{exc}"
            ) from exc
        return call, record

    def _clear_current_tool(self, state: AgentState) -> None:
        state.current_tool_call_id = None
        state.current_tool_name = None

    def confirm_tool_executed(
        self,
        state: AgentState,
        call_id: str,
        result: str,
        *,
        root,
        verified: bool = False,
        success: bool = True,
    ) -> None:
        """人工确认工具已执行，并补记最终结果。"""
        call, record = self._manual_target(state, call_id)
        if (
            record is None
            or record.phase not in {
                ToolPhase.EXECUTING,
                ToolPhase.UNKNOWN,
            }
        ):
            raise ApprovalError(
                "只有 EXECUTING/UNKNOWN 的工具可以补记执行结果"
            )

        name, args = parse_tool_call(call)
        validate_executed(
            name,
            args,
            root,
            result,
            verified=verified,
            success=success,
        )

        phase = (
            ToolPhase.SUCCEEDED if success else ToolPhase.FAILED
        )
        state.status = RunStatus.INTERRUPTED
        state.error = None
        self._clear_current_tool(state)

        self.store.commit(
            state,
            "tool.manual_executed",
            {
                "call_id": call_id,
                "name": name,
                "success": success,
            },
            tool_phase=(call_id, phase, name),
            tool_result=result,
            tool_overwrite_result=True,
        )

    def confirm_tool_not_executed(
        self,
        state: AgentState,
        call_id: str,
        *,
        root,
        verified: bool = False,
        expected_content: str | None = None,
    ) -> None:
        """人工确认工具未执行，允许安全重试。"""
        call, record = self._manual_target(state, call_id)
        if (
            record is not None
            and record.phase
            not in {
                ToolPhase.PREPARED,
                ToolPhase.SKIPPED,
                ToolPhase.EXECUTING,
                ToolPhase.UNKNOWN,
                ToolPhase.FAILED,
            }
        ):
            raise ApprovalError(
                "该工具已成功执行，不能按未执行重试"
            )

        name, args = parse_tool_call(call)
        validate_not_executed(
            name,
            args,
            root,
            verified=verified,
            expected_content=expected_content,
        )

        state.status = RunStatus.INTERRUPTED
        state.error = None
        self._clear_current_tool(state)

        self.store.commit(
            state,
            "tool.manual_not_executed",
            {
                "call_id": call_id,
                "name": name,
                "verified": bool(verified),
            },
            tool_phase=(
                call_id,
                ToolPhase.MANUAL_RETRY,
                name,
            ),
            tool_result="人工确认未执行，允许重试",
            tool_overwrite_result=True,
        )

    def abandon(self, state: AgentState, reason: str) -> None:
        """放弃恢复：终止 Run，但不修改工作区或删除审计数据。"""
        if state.status not in MANUAL_REVIEW_STATUSES:
            raise ApprovalError(
                "只有 WAITING_APPROVAL/INTERRUPTED Run "
                "可以放弃恢复"
            )

        state.status = RunStatus.FAILED
        state.error = str(reason)
        state.metadata["abandoned"] = True
        self._clear_current_tool(state)

        self.store.commit(
            state,
            "run.abandoned",
            {"reason": str(reason)},
        )
        self._prune_after_terminal(state)

    def prune_checkpoints(
        self,
        *,
        keep_per_run: int = 5,
        run_id: str | None = None,
    ) -> int:
        return self.store.prune_checkpoints(
            keep_per_run=keep_per_run,
            run_id=run_id,
        )

    def _prune_after_terminal(self, state: AgentState) -> None:
        try:
            self.prune_checkpoints(
                keep_per_run=5,
                run_id=state.run_id,
            )
        except Exception as exc:
            print(f"[State] Checkpoint 清理失败：{exc}")

    def set_iteration(
        self,
        state: AgentState,
        step: int,
        *,
        messages: list[dict] | None = None,
        pending_tool_calls: list[dict] | None = None,
        next_tool_index: int = 0,
    ) -> None:
        state.iteration = step
        state.metadata["phase"] = "iteration_started"
        checkpoint = None

        if messages is not None:
            checkpoint = self.checkpoints.build(
                state.run_id,
                messages=messages,
                step=step,
                pending_tool_calls=pending_tool_calls,
                next_tool_index=next_tool_index,
            )
            self.checkpoints.bind(state, checkpoint)

        self.store.commit(
            state,
            "run.iteration",
            {"step": step},
            checkpoint=checkpoint,
        )

    def save_checkpoint(
        self,
        state: AgentState,
        *,
        messages: list[dict],
        step: int | None = None,
        pending_tool_calls: list[dict] | None = None,
        next_tool_index: int = 0,
        event_type: str = "checkpoint.saved",
    ):
        if event_type == "iteration.completed":
            state.metadata["phase"] = "iteration_completed"
        elif event_type == "tools.planned":
            state.metadata["phase"] = "tools_planned"
        elif event_type == "checkpoint.saved":
            state.metadata["phase"] = "checkpoint_saved"

        return self.checkpoints.save(
            state,
            messages=messages,
            step=step,
            pending_tool_calls=pending_tool_calls,
            next_tool_index=next_tool_index,
            event_type=event_type,
        )

    def finish(
        self,
        state: AgentState,
        answer: str,
        success: bool = True,
        *,
        messages: list[dict] | None = None,
    ) -> None:
        state.result = answer
        state.status = (
            RunStatus.COMPLETED
            if success else RunStatus.FAILED
        )
        state.current_tool_call_id = None
        state.current_tool_name = None

        checkpoint = None
        if messages is not None:
            checkpoint = self.checkpoints.build(
                state.run_id,
                messages=messages,
                step=state.iteration,
                pending_tool_calls=[],
                next_tool_index=0,
            )
            self.checkpoints.bind(state, checkpoint)

        self.store.commit(
            state,
            "run.finished",
            {"success": success},
            checkpoint=checkpoint,
        )

        # 简单保留策略：只清理已完成/失败 Run 的旧 Checkpoint。
        self._prune_after_terminal(state)

    def fail(
        self,
        state: AgentState,
        error: str,
        interrupted: bool = False,
        *,
        messages: list[dict] | None = None,
    ) -> None:
        state.error = str(error)
        state.status = (
            RunStatus.INTERRUPTED
            if interrupted else RunStatus.FAILED
        )
        state.current_tool_call_id = None
        state.current_tool_name = None

        checkpoint = None
        if messages is not None:
            checkpoint = self.checkpoints.build(
                state.run_id,
                messages=messages,
                step=state.iteration,
                pending_tool_calls=state.pending_tool_calls,
                next_tool_index=state.resume_cursor,
            )
            self.checkpoints.bind(state, checkpoint)

        self.store.commit(
            state,
            "run.failed",
            {"error": str(error), "interrupted": interrupted},
            checkpoint=checkpoint,
        )

        if not interrupted:
            self._prune_after_terminal(state)

    # ------------------------------------------------------------ 工具日志
    def tool_started(
        self,
        state: AgentState,
        call_id: str,
        name: str,
        arguments: str = "",
        index: int | None = None,
    ) -> None:
        """执行前登记。此时工具尚未运行，恢复时可安全重试。"""
        state.current_tool_call_id = call_id
        state.current_tool_name = name
        if index is not None:
            state.resume_cursor = index

        record = ToolRecord(
            run_id=state.run_id,
            call_id=call_id,
            name=name,
            arguments_json=arguments or "{}",
            phase=ToolPhase.PREPARED,
        )
        self.store.commit(
            state,
            "tool.started",
            {"call_id": call_id, "name": name, "index": index},
            tool=record,
        )

    def tool_executing(self, state: AgentState, call_id: str) -> None:
        """真正进入副作用阶段；进程此后崩溃则结果不确定。"""
        name = state.current_tool_name or ""
        self.store.commit(
            state,
            "tool.executing",
            {"call_id": call_id, "name": name},
            tool_phase=(call_id, ToolPhase.EXECUTING, name),
        )

    def tool_finished(
        self,
        state: AgentState,
        call_id: str,
        outcome,
        result=None,
    ) -> None:
        outcome_name = getattr(outcome, "value", outcome)
        phase = ToolPhase.from_outcome(str(outcome_name))
        name = state.current_tool_name or ""

        state.current_tool_call_id = None
        state.current_tool_name = None

        self.store.commit(
            state,
            "tool.finished",
            {
                "call_id": call_id,
                "name": name,
                "outcome": str(outcome_name),
            },
            tool_phase=(call_id, phase, name),
            tool_result=result,
        )
