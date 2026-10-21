"""RepoMedic MCP Bridge.

MCP Client 在 asyncio 事件循环中运行；原来的同步 run_agent 在工作线程中
运行。线程通过 run_coroutine_threadsafe 把工具调用交给 MCP Client 执行，
因此 Session、Context 和 Verification 的代码可以原样复用。
"""

import asyncio
import json
import sys
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path
from typing import Callable

MCP_REMOTE_TOOLS = frozenset({"list_files", "read_file", "run_tests"})


def to_deepseek_tools(mcp_tools: list) -> list[dict]:
    """将 MCP 工具定义转换为 DeepSeek Function Calling JSON Schema。"""
    tools = []

    for tool in mcp_tools:
        if tool.name not in MCP_REMOTE_TOOLS:
            continue

        # MCP Python SDK 使用 input_schema (snake_case)。
        schema = tool.input_schema
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"MCP 工具 {tool.name} 的参数 Schema 无效")

        tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or f"MCP 工具 {tool.name}",
                "parameters": schema,
            },
        })

    names = {tool["function"]["name"] for tool in tools}
    if names != MCP_REMOTE_TOOLS or len(tools) != len(MCP_REMOTE_TOOLS):
        raise RuntimeError(f"MCP Server 必须提供三个工具，实际得到: {names}")

    return tools


def mcp_result_as_text(result) -> str:
    """将 MCP CallToolResult 转成模型能读取的工具结果。"""
    texts = [
        block.text for block in result.content
        if getattr(block, "type", None) == "text"
    ]
    if texts:
        return "\n".join(texts)

    structured = getattr(result, "structured_content", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)

    return "MCP 工具没有返回可显示文本"


def test_result_to_json(result) -> str:
    # MCP 结构化输出可能是 dict，也可能被 result 键包装。
    payload = getattr(result, "structured_content", None)
    if isinstance(payload, dict):
        payload = payload.get("result", payload)

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError) as exc:
            raise ValueError("MCP 测试结果不是有效 JSON") from exc

    if not isinstance(payload, dict):
        raw = mcp_result_as_text(result)
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise ValueError("MCP 测试结果缺少结构化数据") from exc
        if isinstance(payload, dict):
            payload = payload.get("result", payload)

    if not isinstance(payload, dict):
        raise ValueError("MCP 测试结果必须是 JSON 对象")

    required = {"status", "passed", "returncode", "test_count", "output"}
    if not required.issubset(payload):
        raise ValueError(f"MCP 测试结果缺少字段: {sorted(required - set(payload))}")

    status = payload["status"]
    passed = payload["passed"]
    code = payload["returncode"]
    count = payload["test_count"]
    output = payload["output"]

    if (
        status not in {"PASS", "FAIL", "TIMEOUT", "ERROR"}
        or type(passed) is not bool
        or (code is not None and type(code) is not int)
        or type(count) is not int or count < 0
        or not isinstance(output, str)
    ):
        raise ValueError("MCP 测试结果字段类型错误")

    success = status == "PASS" and code == 0 and count > 0
    if passed != success:
        raise ValueError("MCP 测试结果的通过状态与退出码/测试数量矛盾")

    return json.dumps({
        "status": status,
        "passed": passed,
        "returncode": code,
        "test_count": count,
        "output": output[:6000],
    }, ensure_ascii=False)


async def run_agent_with_mcp(
    *,
    task: str,
    llm_client,
    sessions,
    session_id: str,
    allow_write: bool,
    run_agent_fn: Callable,
    server_path: Path,
) -> str:
    """连接本地 MCP Server，动态发现工具并运行原有 Agent Loop。"""
    # 延迟导入：旧的 Verification 单测不依赖 MCP SDK。
    from mcp import Client, StdioServerParameters

    server_path = Path(server_path).resolve()
    if not server_path.is_file():
        raise FileNotFoundError(f"未找到 MCP Server: {server_path}")

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_path)],
    )

    async with Client(params) as mcp_client:
        listed = await mcp_client.list_tools()
        remote_tools = to_deepseek_tools(listed.tools)
        print("[MCP] 已连接并发现:", [
            tool["function"]["name"] for tool in remote_tools
        ])

        # asyncio 事件循环需要保持可用，才能处理 stdio 网络收发。
        # 所以在后台线程运行同步 DeepSeek SDK 和原来的 Agent Loop。
        loop = asyncio.get_running_loop()

        def execute_remote(name: str, arguments: dict) -> str:
            if name not in MCP_REMOTE_TOOLS:
                raise ValueError(f"禁止调用未经授权的 MCP 工具: {name}")
            if not isinstance(arguments, dict):
                raise ValueError("MCP 参数必须是 JSON 对象")

            print(f"[MCP] 调用 {name}({arguments})")
            future = asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(
                    mcp_client.call_tool(name, arguments),
                    timeout=20,
                ),
                loop,
            )

            try:
                result = future.result(timeout=25)
            except FutureTimeout as exc:
                future.cancel()
                raise ValueError(f"MCP 工具 {name} 超时") from exc
            except Exception as exc:
                # 原有 Agent Loop 会把 ValueError 转成工具失败消息，
                # 然后继续让模型决定如何处理。
                raise ValueError(f"MCP 调用失败: {exc}") from exc

            text = mcp_result_as_text(result)
            if result.is_error:
                raise ValueError(f"MCP 工具执行失败: {text}")

            if name == "run_tests":
                return test_result_to_json(result)

            return text

        return await asyncio.to_thread(
            run_agent_fn,
            task,
            llm_client,
            sessions,
            session_id,
            allow_write,
            remote_tools,
            execute_remote,
        )
