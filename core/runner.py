"""主 Agent Loop：管理消息与轮次，业务状态下沉到各个模块。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from context.context_compressor import CompressionError
from core.repair import RepairState
from core.tool_dispatcher import ToolDispatcher
from state.checkpoint import RESUMABLE_STATUSES
from state.journal import ToolPhase
from state.messages import (
    RecoveryError,
    build_tool_result,
    rebuild_messages,
)
from state.models import RunStatus
from state.recovery import (
    RecoveryAction,
    WRITE_TOOLS,
    decide_recovery,
    is_side_effect_tool,
    requires_manual_review,
)


class StateGuardError(RuntimeError):
    """关键状态未持久化时，禁止执行副作用工具。"""


class ResumeError(RuntimeError):
    """无法恢复 Run 时抛出。"""

    def __init__(
        self,
        run_id: str,
        reason: str,
        *,
        status: RunStatus | None = None,
    ):
        super().__init__(f"无法恢复 Run {run_id}: {reason}")
        self.run_id = run_id
        self.reason = reason
        self.status = status


class ResumeBlocked(ResumeError):
    """需要人工确认或状态不一致，拒绝自动恢复。"""


LOAD_SKILL_TOOL = {
    "type": "function",
    "function": {
        "name": "load_skill",
        "description": (
            "按名称加载一个可用 Skill 的完整 SKILL.md 指令。"
            "当任务符合 Available Skills 中某项描述时调用。"
            "如果没有合适的 Skill，不需要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Skill 的名称",
                }
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class AgentServices:
    root: Path
    system_prompt: str
    tools: list[dict]
    hooks: Any
    skill_loader: Any
    format_skill_context: Callable
    context: Any
    compressor: Any
    run_tool: Callable
    execute_tests: Callable
    restore_original: Callable
    checked_test_result: Callable
    subagent_cls: Any
    mcp_remote_tools: frozenset[str]
    max_steps: int
    max_repair_attempts: int
    max_delegations: int
    subagent_max_steps: int
    subagent_max_tool_calls: int
    subagent_request_timeout: float


def _build_available_tools(
    services: AgentServices,
    allow_write: bool,
    mcp_tools: list[dict] | None,
    skill_catalog: str,
) -> list[dict]:
    # 模型看不到未授权的写工具；Runtime 中还有独立的二次校验。
    available_tools = [
        tool for tool in services.tools
        if (
            allow_write
            or tool["function"]["name"]
            not in {"write_file", "generate_ppt"}
           )
        and (mcp_tools is None or tool["function"]["name"] not in services.mcp_remote_tools)
    ]
    if skill_catalog:
        available_tools.append(LOAD_SKILL_TOOL)
    if mcp_tools is not None:
        discovered_names = {item["function"]["name"] for item in mcp_tools}
        if discovered_names != services.mcp_remote_tools:
            raise ValueError(f"MCP 工具不匹配: {discovered_names}")
        available_tools.extend(mcp_tools)
    return available_tools


def _run_loop(
    *,
    client: Any,
    sessions: Any,
    session_id: str,
    allow_write: bool,
    mcp_tools: list[dict] | None,
    mcp_executor: Callable | None,
    services: AgentServices,
    state_manager: Any | None,
    messages: list[dict],
    state: Any | None,
    repair: RepairState,
    start_step: int,
    resume_state: bool = False,
    initial_checkpoint: bool = False,
    pending_batch: list[Any] | None = None,
    replay: dict[str, str] | None = None,
    resumed_step: int = 0,
) -> str:
    state_enabled = state_manager is not None

    def update_state(method: str, *args, **kwargs):
        """非关键状态日志：失败时降级，不影响 Agent 主流程。"""
        if state is None:
            return None
        try:
            return getattr(state_manager, method)(state, *args, **kwargs)
        except Exception as exc:
            print(f"[State] 状态更新失败（{method}）：{exc}")
            return None

    def critical_state(method: str, *args, **kwargs) -> bool:
        """副作用工具执行前的关键写入：失败则禁止继续。"""
        if not state_enabled:
            return True
        if state is None:
            return False
        try:
            getattr(state_manager, method)(state, *args, **kwargs)
            return True
        except Exception as exc:
            print(f"[State] 关键状态写入失败（{method}）：{exc}")
            return False

    skill_catalog = services.skill_loader.format_skill_catalog()

    if skill_catalog:
        print("[Skill] 已发现可用 Skills，等待 Agent 选择")
    else:
        print("[Skill] 未发现可用 Skill")

    if (mcp_tools is None) != (mcp_executor is None):
        raise ValueError("MCP 工具定义和执行器必须同时提供")

    available_tools = _build_available_tools(
        services,
        allow_write,
        mcp_tools,
        skill_catalog,
    )

    dispatcher = ToolDispatcher(
        hooks=services.hooks,
        repair=repair,
        root=services.root,
        run_tool=services.run_tool,
        client=client,
        definitions=services.tools,
        subagent_cls=services.subagent_cls,
        mcp_executor=mcp_executor,
        mcp_remote_tools=services.mcp_remote_tools,
        max_delegations=services.max_delegations,
        subagent_max_steps=services.subagent_max_steps,
        subagent_max_tool_calls=services.subagent_max_tool_calls,
        subagent_request_timeout=services.subagent_request_timeout,
    )

    def finish(answer: str, *, success: bool = True) -> str:
        messages.append({"role": "assistant", "content": answer})
        sessions.save(session_id, messages)
        update_state(
            "finish", answer, success=success, messages=messages
        )
        return answer

    def rollback(reason: str) -> str:
        return repair.rollback(reason, services.restore_original)

    def limit_result(limit_reached: bool, infrastructure_issue):
        if not limit_reached:
            return None
        if infrastructure_issue is not None:
            return finish(
                "[验证环境异常：修复未完成]\n"
                + rollback(
                    f"测试环境返回 {infrastructure_issue}，"
                    "不能据此认定代码存在 Bug"
                )
                + "\n最后一次测试结果：\n"
                + json.dumps(repair.last_test, ensure_ascii=False)
            )
        return finish(
            "[自动修复失败]\n"
            + rollback(f"连续 {repair.write_attempts} 次修改未通过测试")
            + "\n最后一次测试结果：\n"
            + json.dumps(repair.last_test, ensure_ascii=False)
        )

    def process_batch(
        calls: list[Any],
        step: int,
        *,
        assistant_content: Any = None,
        resumed: bool = False,
        replay_map: dict[str, str] | None = None,
    ) -> tuple[bool, str | None]:
        replay_map = replay_map or {}

        if resumed:
            # 恢复批次：assistant tool_calls 已存在于 Checkpoint 中，
            # 且对应的 planned Checkpoint 已经持久化。
            planned_ok = True
        else:
            # 批量工具调用先登记 assistant 决策，执行后逐项配对返回 ID。
            planned_tool_calls = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.function.name,
                        "arguments": call.function.arguments,
                    },
                }
                for call in calls
            ]
            messages.append({
                "role": "assistant",
                "content": assistant_content,
                "tool_calls": planned_tool_calls,
            })
            # 执行前保存待执行工具；副作用工具要求这一步必须成功。
            planned_ok = critical_state(
                "save_checkpoint",
                messages=messages,
                pending_tool_calls=planned_tool_calls,
                next_tool_index=0,
                event_type="tools.planned",
            )

        limit_reached = False
        infrastructure_issue = None

        for tool_index, call in enumerate(calls):
            call_id = call.id
            name = call.function.name

            # 已有最终结果的工具直接复用日志，不重复执行。
            if call_id in replay_map:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": str(replay_map.get(call_id) or ""),
                })
                print(f"[Resume] 复用已完成工具结果：{name} ({call_id})")
                continue

            tool_output = ""
            tool_outcome = "executed"

            journal_ok = critical_state(
                "tool_started",
                call_id,
                name,
                call.function.arguments,
                index=tool_index,
            )

            if limit_reached:
                tool_output = (
                    "工具未执行：Docker 验证环境异常，系统即将回滚当前修改。"
                    if infrastructure_issue is not None
                    else "工具未执行：已达到最大修复次数，系统即将回滚当前修改。"
                )
                tool_outcome = "skipped"
            else:
                try:
                    args = json.loads(call.function.arguments)
                    if not isinstance(args, dict):
                        raise ValueError("工具参数必须是 JSON 对象")

                    # 参数合法后才进入执行阶段；此后的崩溃视为结果未知。
                    if is_side_effect_tool(name):
                        if not planned_ok or not journal_ok:
                            raise StateGuardError(
                                "工具未执行：Checkpoint 或 Tool Journal "
                                "持久化失败，禁止执行可能有副作用的操作。"
                            )
                        if not critical_state(
                            "tool_executing", call_id
                        ):
                            raise StateGuardError(
                                "工具未执行：工具执行状态无法持久化，"
                                "禁止执行可能有副作用的操作。"
                            )
                    else:
                        update_state("tool_executing", call_id)

                    if name == "load_skill":
                        skill_name = args.get("name")
                        if not isinstance(skill_name, str):
                            raise ValueError("Skill 名称必须是字符串")
                        # 根据名称读取完整 SKILL.md
                        loaded_skill = services.skill_loader.load_by_name(skill_name)
                        # 将完整 Skill 指令作为工具结果返回
                        tool_output = services.format_skill_context(loaded_skill)
                        print(f"[Skill] 按需加载：{loaded_skill.name}")

                    else:
                        raw_output = dispatcher.dispatch(
                            name, args, allow_write=allow_write,
                        )
                        tool_output = (
                            raw_output
                            if isinstance(raw_output, str)
                            else json.dumps(raw_output, ensure_ascii=False,)
                        )
                    if name == "write_file":
                        last_test = repair.after_write(
                            execute_tests=services.execute_tests,
                            checked_result=services.checked_test_result,
                            on_verification_fail=services.hooks.on_verification_fail,
                        )
                        tool_output += (
                            "\n[Python 自动验证]\n"
                            + json.dumps(last_test, ensure_ascii=False)
                        )
                        status = last_test["status"]
                        if status in {"ERROR", "TIMEOUT"}:
                            infrastructure_issue = status
                            limit_reached = True
                        elif repair.attempts_exhausted:
                            limit_reached = True

                    elif name == "run_tests":
                        try:
                            received = json.loads(tool_output)
                        except (ValueError, TypeError):
                            received = None
                        test_result = repair.after_run_tests(
                            received,
                            checked_result=services.checked_test_result,
                            on_verification_fail=services.hooks.on_verification_fail,
                        )
                        if repair.wrote_file and test_result["status"] in {"ERROR", "TIMEOUT"}:
                            infrastructure_issue = test_result["status"]
                            limit_reached = True

                except StateGuardError as exc:
                    tool_output = str(exc)
                    tool_outcome = "skipped"
                    print(f"[StateGuard] {name}: {tool_output}")

                except Exception as exc:
                    tool_output = f"工具错误: {type(exc).__name__}: {exc}"
                    tool_outcome = "raised"
                    print(f"[ToolError] {name}: {tool_output}")

            print(f"[第 {step} 轮] {name}({call.function.arguments})")
            messages.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": tool_output,
            })
            update_state(
                "tool_finished", call_id, tool_outcome, tool_output
            )

        sessions.save(session_id, messages)
        # 整个批次完成后保存干净 Checkpoint，便于下一次 Resume。
        update_state(
            "save_checkpoint",
            messages=messages,
            pending_tool_calls=[],
            next_tool_index=0,
            event_type="iteration.completed",
        )
        return limit_reached, infrastructure_issue

    try:
        if resume_state:
            update_state("resume")
        else:
            update_state("start")

        if initial_checkpoint:
            update_state("save_checkpoint", messages=messages, step=0)

        if pending_batch:
            limit_reached, infrastructure_issue = process_batch(
                pending_batch,
                resumed_step if resumed_step > 0 else start_step,
                resumed=True,
                replay_map=replay,
            )
            result = limit_result(limit_reached, infrastructure_issue)
            if result is not None:
                return result

        for step in range(max(1, start_step), services.max_steps + 1):
            try:
                update_state(
                    "set_iteration", step, messages=messages
                )
                context_messages = services.compressor.build(
                    session_id=session_id,
                    messages=messages,
                    client=client,
                    model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                )
            except CompressionError as exc:
                # 压缩器已完成安全回退；不能再用裁剪静默丢失关键约束
                print(f"[Compression] 无法安全回退：{exc}")
                raise
            except Exception as exc:
                print(f"[Compression] 摘要失败，回退裁剪：{exc}")
                context_messages = services.context.build(messages)

            if skill_catalog:
                if not context_messages or context_messages[0].get("role") != "system":
                    raise RuntimeError("缺少 system 消息")
                context_messages = list(context_messages)
                context_messages[0] = {
                    **context_messages[0],
                    "content": context_messages[0]["content"] + "\n\n" + skill_catalog,
                }

            print(
                f"[Context] 完整历史: {len(messages)} 条，"
                f"实际发送: {len(context_messages)} 条"
            )

            response = client.chat.completions.create(
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                messages=context_messages,
                tools=available_tools,
                tool_choice="auto",
                extra_body={"thinking": {"type": "disabled"}},
            )
            if response.usage is not None:
                print(
                    f"[Token] 输入: {response.usage.prompt_tokens}，"
                    f"输出: {response.usage.completion_tokens}"
                )
                record = getattr(
                    services.compressor, "record_main_usage", None
                )

                if callable(record):
                    record(response.usage)

                report_fn = getattr(
                    services.compressor, "token_report", None
                )
                report = report_fn() if callable(report_fn) else None

                if isinstance(report, dict):
                    print(
                        "[Token] 累计: 主模型 "
                        f"{report['main_input']}/"
                        f"{report['main_output']}，摘要 "
                        f"{report['summary_input']}/"
                        f"{report['summary_output']}，"
                        f"压缩 {report['compactions']} 次，"
                        f"Micro Compact {report['micro_compactions']} 次，"
                        f"总计 {report['total_tokens']}"
                    )

            choice = response.choices[0]
            message = choice.message
            calls = message.tool_calls or []

            if not calls:
                if choice.finish_reason != "stop":
                    raise RuntimeError(f"模型未正常完成：{choice.finish_reason}")
                answer = message.content or "模型没有返回文本"

                if repair.original_bytes is not None and not repair.verified_passed:
                    if repair.should_retry_prompt(step, services.max_steps):
                        repair.retry_prompts += 1
                        messages.append({"role": "assistant", "content": answer})
                        messages.append({
                            "role": "user",
                            "content": (
                                "[Agent验证控制器]上次修改没有通过测试。"
                                "请根据下列真实日志再次尝试修复，不要只口头宣布成功。"
                                "如果无法修复，请明确说明。\n"
                                + json.dumps(repair.last_test, ensure_ascii=False)
                            ),
                        })
                        sessions.save(session_id, messages)
                        print("[Verification] 测试未通过，要求模型继续修复")
                        continue

                    return finish(
                        "[修复失败]\n"
                        + rollback("模型提前结束或不再提供修复方案")
                        + "\n最后一次测试结果：\n"
                        + json.dumps(repair.last_test, ensure_ascii=False)
                    )

                if repair.wrote_file and repair.verified_passed:
                    return finish(
                        f"[Python 验证成功]共修改 {repair.write_attempts} 次，"
                        f"通过 {repair.last_test['test_count']} 个单元测试，退出码为 0。\n\n"
                        + answer
                    )
                return finish("[本轮没有修改文件]\n" + answer if allow_write else answer)

            limit_reached, infrastructure_issue = process_batch(
                calls,
                step,
                assistant_content=message.content,
            )
            result = limit_result(limit_reached, infrastructure_issue)
            if result is not None:
                return result

        message = f"模型调用达到 {services.max_steps} 轮，任务已停止。"
        if repair.original_bytes is not None and not repair.verified_passed:
            message += "\n" + rollback("达到模型调用轮数限制")
        elif repair.wrote_file and repair.verified_passed:
            message += "\n最新版本通过真实单元测试，但模型未完成最终报告。"
        return finish(message)

    except BaseException as exc:
        if repair.original_bytes is not None:
            try:
                rollback("Agent 执行异常")
            except Exception as rollback_exc:
                print("[Rollback] 自动恢复失败："f"{rollback_exc}")
        update_state(
            "fail",
            error=repr(exc),
            interrupted=isinstance(exc, KeyboardInterrupt),
            messages=messages,
        )
        raise


def run_agent(
    task: str,
    client: Any,
    sessions: Any,
    session_id: str,
    allow_write: bool = False,
    mcp_tools: list[dict] | None = None,
    mcp_executor: Callable | None = None,
    *,
    services: AgentServices,
    state_manager: Any | None = None,
) -> str:
    messages = sessions.load(session_id)
    state = None
    if state_manager is not None:
        try:
            state = state_manager.create(
                session_id=session_id, task=task
            )
        except Exception as exc:
            # 状态持久化是旁路能力，失败不应阻断原有 Agent 逻辑。
            print(f"[State] 无法创建运行记录：{exc}")

    if state is not None:
        # 供 Resume 恢复原始写权限；start() 会把 metadata 一起提交。
        state.metadata["allow_write"] = allow_write

    if not messages or messages[0].get("role") != "system":
        raise ValueError("Session 缺少 system 消息")
    messages[0]["content"] = services.system_prompt
    messages.append({"role": "user", "content": task})

    return _run_loop(
        client=client,
        sessions=sessions,
        session_id=session_id,
        allow_write=allow_write,
        mcp_tools=mcp_tools,
        mcp_executor=mcp_executor,
        services=services,
        state_manager=state_manager,
        messages=messages,
        state=state,
        repair=RepairState(max_attempts=services.max_repair_attempts),
        start_step=1,
        resume_state=False,
        initial_checkpoint=True,
    )


# ------------------------------------------------------------ Resume 辅助
def _decode_record_output(record) -> str | None:
    if record is None or record.result_json is None:
        return None
    try:
        value = json.loads(record.result_json)
    except (TypeError, ValueError):
        return None
    if isinstance(value, dict) and "output" in value:
        value = value["output"]
    return value if isinstance(value, str) else None


def _parse_backup_path(output: str) -> Path | None:
    marker = "备份位置:"
    if marker not in output:
        return None
    tail = output.split(marker, 1)[1]
    raw = tail.split("。", 1)[0].strip()
    return Path(raw) if raw else None


def _parse_verification(output: str) -> dict | None:
    marker = "\n[Python 自动验证]\n"
    if marker not in output:
        return None
    raw = output.rsplit(marker, 1)[1]
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _count_retry_prompts(messages: list[dict]) -> int:
    return sum(
        1
        for message in messages
        if (
            isinstance(message, dict)
            and message.get("role") == "user"
            and str(message.get("content", "")).startswith(
                "[Agent验证控制器]"
            )
        )
    )


def _restore_repair_state(
    services: AgentServices,
    records: list,
    messages: list[dict],
) -> tuple[RepairState | None, str | None]:
    """从写工具日志恢复 Repair 快照；无法安全恢复时返回原因。"""
    writes = [
        record for record in records
        if record.name == "write_file"
        and record.phase == ToolPhase.SUCCEEDED
    ]
    repair = RepairState(
        max_attempts=services.max_repair_attempts,
        retry_prompts=_count_retry_prompts(messages),
    )
    if not writes:
        failed_writes = [
            record for record in records
            if record.name == "write_file"
            and record.phase == ToolPhase.FAILED
        ]
        if failed_writes:
            return None, (
                "检测到失败的写工具且没有可用快照，"
                "无法确认工作区是否被修改，拒绝自动恢复"
            )
        return repair, None

    first_output = _decode_record_output(writes[0])
    if first_output is None:
        return None, "无法读取首次写工具的日志结果"

    backup = _parse_backup_path(first_output)
    if (
        backup is None
        or not backup.is_absolute()
        or backup.is_symlink()
        or not backup.is_file()
        or backup.suffix != ".bak"
        or backup.parent.name != "backups"
    ):
        return None, "无法校验写工具备份快照，拒绝自动恢复"

    try:
        original_bytes = backup.read_bytes()
    except OSError as exc:
        return None, f"无法读取备份快照：{exc}"

    last = writes[-1]
    last_output = _decode_record_output(last)
    if last_output is None:
        return None, "无法读取最后一次写工具的日志结果"

    last_test = _parse_verification(last_output)
    if last_test is None:
        return None, "最后一次写工具缺少可解析的验证结果"
    if last_test.get("status") in {"ERROR", "TIMEOUT"}:
        return None, "最后一次验证环境异常，拒绝自动恢复"

    try:
        args = json.loads(last.arguments_json)
    except (TypeError, ValueError):
        return None, "最后一次写工具参数无法解析"
    if not isinstance(args, dict):
        return None, "最后一次写工具参数格式错误"

    path = args.get("path")
    content = args.get("content")
    if path != "calc.py" or not isinstance(content, str):
        return None, "最后一次写工具参数不完整，拒绝自动恢复"

    root = Path(services.root).resolve()
    target = (root / path).resolve()
    if (
        not target.is_relative_to(root)
        or target.is_symlink()
        or not target.is_file()
    ):
        return None, "工作区目标文件不可安全恢复"

    try:
        current = target.read_bytes()
    except OSError as exc:
        return None, f"无法读取工作区文件：{exc}"
    if current != content.encode("utf-8"):
        return None, "工作区内容与最后一次写入不一致，拒绝自动恢复"

    if (
        len(writes) >= services.max_repair_attempts
        and not last_test.get("passed")
    ):
        return None, "修复次数已耗尽且未通过验证，拒绝自动恢复"

    repair.original_bytes = original_bytes
    repair.write_attempts = len(writes)
    repair.wrote_file = True
    repair.verified_passed = bool(last_test.get("passed"))
    repair.last_test = last_test
    return repair, None


def _pending_to_calls(pending: list[dict]) -> list[SimpleNamespace]:
    calls: list[SimpleNamespace] = []
    for call in pending:
        if not isinstance(call, dict):
            raise RecoveryError("待执行工具格式错误")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id:
            raise RecoveryError("待执行工具缺少有效 call_id")
        if not isinstance(function, dict):
            raise RecoveryError(f"待执行工具 {call_id} 缺少 function")
        name = function.get("name")
        arguments = function.get("arguments")
        if not isinstance(name, str) or not name:
            raise RecoveryError(f"待执行工具 {call_id} 缺少工具名称")
        if not isinstance(arguments, str):
            raise RecoveryError(f"待执行工具 {call_id} 的参数不是字符串")
        calls.append(SimpleNamespace(
            id=call_id,
            function=SimpleNamespace(
                name=name,
                arguments=arguments,
            ),
        ))
    return calls


def _prepare_resume_messages(
    checkpoints: Any,
    checkpoint: Any,
    records: dict[str, Any],
    run_id: str,
) -> tuple[list[dict], dict[str, str]]:
    pending = checkpoint.pending_tool_calls
    if not pending:
        return checkpoints.prepare_messages(run_id), {}

    calls = _pending_to_calls(pending)
    pending_ids = [call.id for call in calls]

    batch_index = None
    for index in range(len(checkpoint.messages) - 1, -1, -1):
        message = checkpoint.messages[index]
        if (
            isinstance(message, dict)
            and message.get("role") == "assistant"
            and message.get("tool_calls")
        ):
            ids = [
                call.get("id")
                for call in message["tool_calls"]
                if isinstance(call, dict)
            ]
            if ids == pending_ids:
                batch_index = index
                break

    if batch_index is None:
        raise RecoveryError(
            "Checkpoint 待执行工具与 Messages 中的 tool_calls 不匹配"
        )

    prefix = checkpoint.messages[:batch_index]
    assistant_message = checkpoint.messages[batch_index]

    existing: dict[str, dict] = {}
    index = batch_index + 1
    while index < len(checkpoint.messages):
        message = checkpoint.messages[index]
        if not isinstance(message, dict) or message.get("role") != "tool":
            break
        call_id = message.get("tool_call_id")
        if call_id not in pending_ids:
            raise RecoveryError(
                f"tool 消息 {call_id!r} 没有对应的待执行工具"
            )
        if call_id in existing:
            raise RecoveryError(
                f"tool_call {call_id} 存在重复的 tool 结果"
            )
        existing[call_id] = message
        index += 1

    if index != len(checkpoint.messages):
        raise RecoveryError(
            "待执行工具之后存在后续消息，无法安全恢复"
        )

    rebuilt_prefix = rebuild_messages(prefix, records)

    replay: dict[str, str] = {}
    for call in calls:
        if call.id in existing:
            replay[call.id] = str(
                existing[call.id].get("content") or ""
            )
            continue

        record = records.get(call.id)
        action = decide_recovery(record)
        if action == RecoveryAction.REPLAY_RESULT:
            replay[call.id] = build_tool_result(record, call.id)
        elif action in {
            RecoveryAction.REQUIRE_APPROVAL,
            RecoveryAction.RECONCILE,
        }:
            raise RecoveryError(
                f"工具 {call.function.name}（{call.id}）"
                "需要人工确认，无法自动恢复"
            )

    return rebuilt_prefix + [assistant_message], replay


def _block_resume(
    state_manager: Any,
    state: Any,
    run_id: str,
    reason: str,
) -> None:
    try:
        state_manager.wait_for_approval(state, reason)
    except Exception as exc:
        print(f"[Resume] 无法保存待确认状态：{exc}")
    raise ResumeBlocked(
        run_id,
        reason,
        status=RunStatus.WAITING_APPROVAL,
    )


def resume_agent(
    run_id: str,
    client: Any,
    sessions: Any,
    allow_write: bool | None = None,
    mcp_tools: list[dict] | None = None,
    mcp_executor: Callable | None = None,
    *,
    services: AgentServices,
    state_manager: Any | None = None,
) -> str:
    """恢复已有 Run，不创建新的 run_id。"""
    if state_manager is None:
        raise ResumeError(run_id, "未启用状态管理")

    try:
        state = state_manager.load(run_id)
    except (ValueError, KeyError, TypeError) as exc:
        raise ResumeError(
            run_id, f"运行状态数据损坏，无法恢复：{exc}"
        ) from exc
    if state is None:
        raise ResumeError(run_id, "找不到运行记录")

    if state.status not in RESUMABLE_STATUSES:
        raise ResumeError(
            run_id,
            f"当前状态 {state.status.value} 不可恢复",
        )

    checkpoints = state_manager.checkpoints
    try:
        checkpoint = checkpoints.latest(run_id)
    except (ValueError, KeyError, TypeError) as exc:
        raise ResumeError(
            run_id, f"Checkpoint 数据损坏，无法恢复：{exc}"
        ) from exc
    if checkpoint is None:
        raise ResumeError(run_id, "没有可用的 Checkpoint")

    try:
        records_list = state_manager.store.load_tools(run_id)
    except (ValueError, KeyError, TypeError) as exc:
        raise ResumeError(
            run_id, f"Tool Journal 数据损坏，无法恢复：{exc}"
        ) from exc
    records = {
        record.call_id: record
        for record in records_list
    }

    try:
        inspection = checkpoints.inspect_batch(run_id)
    except (ValueError, KeyError, TypeError) as exc:
        raise ResumeError(
            run_id, f"Checkpoint 数据损坏，无法恢复：{exc}"
        ) from exc
    if inspection.issues:
        _block_resume(
            state_manager,
            state,
            run_id,
            "Checkpoint 与 Tool Journal 不一致："
            + "；".join(inspection.issues),
        )

    # 只有终态工具可以直接复用；无法核对副作用的工具默认人工处理。
    for call in checkpoint.pending_tool_calls:
        call_id = call.get("id") if isinstance(call, dict) else None
        function = call.get("function") if isinstance(call, dict) else None
        name = function.get("name") if isinstance(function, dict) else None
        record = records.get(call_id)

        if (
            record is not None
            and record.phase in {ToolPhase.EXECUTING, ToolPhase.UNKNOWN}
            and is_side_effect_tool(name or "")
        ):
            reason = (
                f"工具 {name}({call_id}) 处于 "
                f"{record.phase.value} 状态，副作用未知，"
                "需要人工确认后才能恢复。"
            )
            if requires_manual_review(name or ""):
                reason += " 该工具无法自动核对副作用，请使用人工处理入口。"
            _block_resume(
                state_manager, state, run_id, reason
            )

        if (
            requires_manual_review(name or "")
            and (
                record is None
                or record.phase in {
                    ToolPhase.PREPARED,
                    ToolPhase.SKIPPED,
                }
            )
        ):
            _block_resume(
                state_manager,
                state,
                run_id,
                f"工具 {name}({call_id}) 的副作用无法自动核对，"
                "默认禁止自动重试；请使用人工处理入口确认后再恢复。",
            )

    if allow_write is None:
        stored = state.metadata.get("allow_write")
        if isinstance(stored, bool):
            allow_write = stored
        else:
            # 旧 Run 没有持久化权限；已有成功写入可证明曾经授权。
            allow_write = any(
                record.name in WRITE_TOOLS
                and record.phase == ToolPhase.SUCCEEDED
                for record in records_list
            )

    try:
        messages, replay = _prepare_resume_messages(
            checkpoints, checkpoint, records, run_id
        )
    except RecoveryError as exc:
        _block_resume(
            state_manager, state, run_id, str(exc)
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise ResumeError(
            run_id, f"恢复 Messages 数据损坏：{exc}"
        ) from exc

    if messages and messages[0].get("role") == "system":
        messages[0]["content"] = services.system_prompt

    repair, reason = _restore_repair_state(
        services, records_list, messages
    )
    if reason is not None:
        _block_resume(state_manager, state, run_id, reason)

    latest_event = state_manager.store.latest_checkpoint_event(run_id)
    event_type = (
        latest_event.get("event_type")
        if latest_event is not None
        else None
    )

    pending_calls = _pending_to_calls(checkpoint.pending_tool_calls)
    resumed_step = checkpoint.step
    if pending_calls:
        start_step = checkpoint.step + 1
    else:
        phase = state.metadata.get("phase")
        if phase == "iteration_started":
            # 上一次模型调用前中断：重试同一轮。
            start_step = checkpoint.step
        elif phase == "iteration_completed":
            start_step = checkpoint.step + 1
        elif phase == "tools_planned":
            start_step = checkpoint.step + 1
        elif event_type == "run.iteration":
            # 兼容没有 phase 标记的旧 Run。
            start_step = checkpoint.step
        else:
            start_step = checkpoint.step + 1

    checkpoints.bind(state, checkpoint)
    state.iteration = checkpoint.step

    return _run_loop(
        client=client,
        sessions=sessions,
        session_id=state.session_id,
        allow_write=allow_write,
        mcp_tools=mcp_tools,
        mcp_executor=mcp_executor,
        services=services,
        state_manager=state_manager,
        messages=messages,
        state=state,
        repair=repair,
        start_step=max(1, start_step),
        resume_state=True,
        initial_checkpoint=False,
        pending_batch=pending_calls or None,
        replay=replay,
        resumed_step=resumed_step,
    )
