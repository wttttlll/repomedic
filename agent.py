"""RepoMedic CLI 入口与向后兼容接口。

核心逻辑在 core/ 和 tools/：
- core.runner: Agent Loop
- core.tool_dispatcher: Hook / MCP / Sub-agent 路由
- core.repair: 快照、重试与回滚
- core.verification: Docker 测试证据校验
- tools.local_tools: 本地文件工具
- tools.definitions: Function Calling Schema

保留原 run_agent / run_tool / execute_tests 等入口，以兼容现有测试和 Benchmark。
"""
from __future__ import annotations
# from integrations.ppt_generator import generate_ppt
import os

from config import (
    BASE,
    ROOT,
    MAX_FILE_BYTES,
    MAX_STEPS,
    MAX_REPAIR_ATTEMPTS,
    MAX_DELEGATIONS_PER_TASK,
    SUBAGENT_MAX_STEPS,
    SUBAGENT_MAX_TOOL_CALLS,
    SUBAGENT_REQUEST_TIMEOUT,
    MCP_REMOTE_TOOLS,
    SYSTEM_PROMPT,
)
from context.context_compressor import ContextCompressor
from context.context_manager import ContextManager
from core.repair import restore_file
from core.runner import AgentServices, run_agent as _run_agent_loop
from core.verification import checked_test_result as _verify_result
from core.verification import execute_tests as _docker_execute_tests
from hooks.hook_manager import HookManager
from integrations.test_runner import run_unittest
from sessions.session_manager import SessionManager
from skills.skill_loader import SkillLoader, format_skill_context
from subagents.test_analyst import TestAnalystSubAgent
from tools.definitions import TOOLS
from tools.local_tools import run_tool as _run_local_tool

SKILL_LOADER = SkillLoader(BASE / "skills")
HOOKS = HookManager()
CONTEXT = ContextManager(max_turns=4, max_chars=37_000)
COMPRESSOR = ContextCompressor(
    memory_dir=BASE / "data" / "summary_data",
    selector=CONTEXT,
    total_budget=40_000,
    max_summary_chars=1_200,
)


# 兼容层：这些 wrapper 读取 agent 模块的当前全局引用。
# 现有单元测试对 agent.ROOT / agent.run_tool / agent.execute_tests 的 patch 仍能生效。
def _checked_test_result(value: object) -> dict:
    return _verify_result(value)


def execute_tests() -> dict:
    return _docker_execute_tests(ROOT, runner=run_unittest, checked_result=_checked_test_result)


def restore_original(content: bytes) -> None:
    restore_file(ROOT, content)


def run_tool(name: str, args: dict, allow_write: bool = False) -> str:
    return _run_local_tool(
        name,
        args,
        allow_write=allow_write,
        root=ROOT,
        base=BASE,
        max_file_bytes=MAX_FILE_BYTES,
        execute_tests=execute_tests,
    )


def run_agent(
    task: str,
    client,
    sessions: SessionManager,
    session_id: str,
    allow_write: bool = False,
    mcp_tools: list[dict] | None = None,
    mcp_executor=None,
) -> str:
    """向后兼容的 Agent API；真正循环由 core.runner.run_agent 执行。"""
    services = AgentServices(
        root=ROOT,
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        hooks=HOOKS,
        skill_loader=SKILL_LOADER,
        format_skill_context=format_skill_context,
        context=CONTEXT,
        compressor=COMPRESSOR,
        run_tool=run_tool,
        execute_tests=execute_tests,
        restore_original=restore_original,
        checked_test_result=_checked_test_result,
        subagent_cls=TestAnalystSubAgent,
        mcp_remote_tools=MCP_REMOTE_TOOLS,
        max_steps=MAX_STEPS,
        max_repair_attempts=MAX_REPAIR_ATTEMPTS,
        max_delegations=MAX_DELEGATIONS_PER_TASK,
        subagent_max_steps=SUBAGENT_MAX_STEPS,
        subagent_max_tool_calls=SUBAGENT_MAX_TOOL_CALLS,
        subagent_request_timeout=SUBAGENT_REQUEST_TIMEOUT,
    )
    return _run_agent_loop(
        task=task,
        client=client,
        sessions=sessions,
        session_id=session_id,
        allow_write=allow_write,
        mcp_tools=mcp_tools,
        mcp_executor=mcp_executor,
        services=services,
    )


def main() -> None:
    import asyncio
    from integrations.mcp_bridge import run_agent_with_mcp
    from openai import OpenAI

    key = os.getenv("apikey") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise SystemExit("请先设置 apikey 或 DEEPSEEK_API_KEY 环境变量")

    client = OpenAI(
        api_key=key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    sessions = SessionManager(BASE / "data" / "session_data")
    session_id = input("请输入旧 Session ID（直接回车创建新会话）：").strip()
    if session_id:
        try:
            sessions.load(session_id)
        except (FileNotFoundError, ValueError) as exc:
            raise SystemExit(f"无法恢复 Session: {exc}") from exc
    else:
        session_id = sessions.create(SYSTEM_PROMPT)

    print(f"\n当前 Session ID: {session_id}")
    print("输入 exit 结束会话。\n")

    while True:
        try:
            print("请输入任务（支持多行，单独输入 END 结束）：")
            lines = []
            while True:
                line = input()
                if line.strip() == "END":
                    break
                lines.append(line)
            task = "\n".join(lines).strip()
            
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break

        if task.lower() in {"exit", "quit"}:
            print("已退出。")
            break
        if not task:
            continue
        mode = input("请选择模式（1=只读诊断，2=授权修改文件并自动测试）：").strip()
        if mode not in {"1", "2"}:
            print("模式无效，请输入 1 或 2。")
            continue

        try:
            result = asyncio.run(run_agent_with_mcp(
                task=task,
                llm_client=client,
                sessions=sessions,
                session_id=session_id,
                allow_write=(mode == "2"),
                run_agent_fn=run_agent,
                server_path=BASE / "integrations" / "mcp_server.py",
            ))
            print("\nAgent 最终回答：")
            print(result)
        except Exception as exc:
            import traceback
            traceback.print_exception(exc)


if __name__ == "__main__":
    main()
