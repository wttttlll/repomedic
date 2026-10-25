"""RepoMedic 主 Agent Loop：管理消息与轮次，业务状态下沉到各个模块。"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from core.repair import RepairState
from core.tool_dispatcher import ToolDispatcher


@dataclass(frozen=True)
class AgentServices:
    """显式注入依赖；测试可继续 patch agent.py 的旧入口。"""
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
) -> str:
    """原 `agent.run_agent` 同语义的同步 Agent Loop。"""
    messages = sessions.load(session_id)
    if not messages or messages[0].get("role") != "system":
        raise ValueError("Session 缺少 system 消息")
    messages[0]["content"] = services.system_prompt
    messages.append({"role": "user", "content": task})

    active_skill = services.skill_loader.select(task)
    skill_context = None
    if active_skill is not None:
        skill_context = services.format_skill_context(active_skill)
        print(f"[Skill] 加载： {active_skill.name}")
    else:
        print("[Skill] 无匹配的skill")

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
    if (mcp_tools is None) != (mcp_executor is None):
        raise ValueError("MCP 工具定义和执行器必须同时提供")
    if mcp_tools is not None:
        discovered_names = {item["function"]["name"] for item in mcp_tools}
        if discovered_names != services.mcp_remote_tools:
            raise ValueError(f"MCP 工具不匹配: {discovered_names}")
        available_tools.extend(mcp_tools)

    repair = RepairState(max_attempts=services.max_repair_attempts)
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

    def finish(answer: str) -> str:
        messages.append({"role": "assistant", "content": answer})
        sessions.save(session_id, messages)
        return answer

    def rollback(reason: str) -> str:
        return repair.rollback(reason, services.restore_original)

    try:
        for step in range(1, services.max_steps + 1):
            try:
                context_messages = services.compressor.build(
                    session_id=session_id,
                    messages=messages,
                    client=client,
                    model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
                )
            except Exception as exc:
                print(f"[Compression] 摘要失败，回退裁剪：{exc}")
                context_messages = services.context.build(messages)

            if skill_context is not None:
                if not context_messages or context_messages[0].get("role") != "system":
                    raise RuntimeError("缺少 system 消息")
                context_messages = list(context_messages)
                context_messages[0] = {
                    **context_messages[0],
                    "content": context_messages[0]["content"] + "\n\n" + skill_context,
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

            # 批量工具调用先登记 assistant 决策，执行后逐项配对返回 ID。
            messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    {
                        "id": call.id,
                        "type": "function",
                        "function": {
                            "name": call.function.name,
                            "arguments": call.function.arguments,
                        },
                    }
                    for call in calls
                ],
            })

            limit_reached = False
            infrastructure_issue = None
            for call in calls:
                name = call.function.name
                tool_output = ""

                if limit_reached:
                    tool_output = (
                        "工具未执行：Docker 验证环境异常，系统即将回滚当前修改。"
                        if infrastructure_issue is not None
                        else "工具未执行：已达到最大修复次数，系统即将回滚当前修改。"
                    )
                else:
                    try:
                        args = json.loads(call.function.arguments)
                        if not isinstance(args, dict):
                            raise ValueError("工具参数必须是 JSON 对象")

                        raw_output = dispatcher.dispatch(name, args, allow_write=allow_write)
                        tool_output = (
                            raw_output
                            if isinstance(raw_output, str)
                            else json.dumps(raw_output, ensure_ascii=False)
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

                    except Exception as exc:
                        tool_output = f"工具错误: {type(exc).__name__}: {exc}"
                        print(f"[ToolError] {name}: {tool_output}")

                print(f"[第 {step} 轮] {name}({call.function.arguments})")
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": tool_output,
                })

            sessions.save(session_id, messages)

            if limit_reached:
                if infrastructure_issue is not None:
                    return finish(
                        "【验证环境异常：修复未完成】\n"
                        + rollback(
                            f"测试环境返回 {infrastructure_issue}，"
                            "不能据此认定代码存在 Bug"
                        )
                        + "\n最后一次测试结果：\n"
                        + json.dumps(repair.last_test, ensure_ascii=False)
                    )
                return finish(
                    "【自动修复失败】\n"
                    + rollback(f"连续 {repair.write_attempts} 次修改未通过测试")
                    + "\n最后一次测试结果：\n"
                    + json.dumps(repair.last_test, ensure_ascii=False)
                )

        message = f"模型调用达到 {services.max_steps} 轮，任务已停止。"
        if repair.original_bytes is not None and not repair.verified_passed:
            message += "\n" + rollback("达到模型调用轮数限制")
        elif repair.wrote_file and repair.verified_passed:
            message += "\n最新版本通过真实单元测试，但模型未完成最终报告。"
        return finish(message)

    except BaseException:
        # 包括 API/工具异常以及 KeyboardInterrupt，未通过的写入仍需回滚。
        if repair.original_bytes is not None:
            try:
                rollback("Agent 执行异常")
            except Exception as exc:
                print(f"[Rollback] 自动恢复失败，请检查 backups：{exc}")
        raise
