"""根据工具执行日志决定恢复策略。

核心安全规则：写工具一旦进入 EXECUTING / FAILED，就无法确定是否已经产生
副作用，必须人工确认，禁止自动重试。
"""
from __future__ import annotations

from enum import Enum

from .journal import ToolPhase, ToolRecord


class RecoveryAction(str, Enum):
    REPLAY_RESULT = "replay_result"
    RETRY = "retry"
    RECONCILE = "reconcile"
    REQUIRE_APPROVAL = "require_approval"


READ_ONLY_TOOLS = frozenset({
    "read_file",
    "list_files",
    "search_files",
    "run_tests",
    "load_skill",
    "inspect_ppt_template",
    "delegate_test_analysis",
})

WRITE_TOOLS = frozenset({"write_file", "generate_ppt"})

# 只有 write_file 能通过备份/目标文件内容可靠核对副作用。
VERIFIABLE_SIDE_EFFECT_TOOLS = frozenset({"write_file"})


def is_side_effect_tool(name: str) -> bool:
    """未知工具按可能有副作用处理，只读工具白名单才允许降级。"""
    return name not in READ_ONLY_TOOLS


def requires_manual_review(name: str) -> bool:
    """无法自动核对副作用的工具，默认禁止自动重试。"""
    return (
        is_side_effect_tool(name)
        and name not in VERIFIABLE_SIDE_EFFECT_TOOLS
    )


def decide_recovery(record: ToolRecord | None) -> RecoveryAction:
    """给出单个工具的恢复动作。"""
    if record is None:
        # 只有登记的调用才会产生记录；没有记录说明它从未开始执行。
        return RecoveryAction.RETRY

    if record.phase in {ToolPhase.SUCCEEDED, ToolPhase.FAILED}:
        # 已有最终结果（成功或明确的失败），直接复用，不重复执行。
        return RecoveryAction.REPLAY_RESULT

    if record.phase in {
        ToolPhase.PREPARED,
        ToolPhase.SKIPPED,
        ToolPhase.MANUAL_RETRY,
    }:
        # 这些状态确定没有产生副作用（或已由人工确认）。
        return RecoveryAction.RETRY

    if record.name in READ_ONLY_TOOLS:
        # 只读工具重复执行是安全的。
        return RecoveryAction.RETRY

    # EXECUTING / UNKNOWN，且可能是 write_file、generate_ppt
    # 或未知工具：副作用状态不确定，必须人工确认。
    return RecoveryAction.REQUIRE_APPROVAL
