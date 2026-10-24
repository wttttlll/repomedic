"""所有工具调用的唯一分发位置：Hooks → MCP / Local / Sub-agent。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

from core.repair import RepairState


class ToolDispatcher:
    def __init__(
        self,
        *,
        hooks: Any,
        repair: RepairState,
        root: Path,
        run_tool: Callable,
        client: Any,
        definitions: list[dict],
        subagent_cls: Any,
        mcp_executor: Callable | None,
        mcp_remote_tools: frozenset[str],
        max_delegations: int,
        subagent_max_steps: int,
        subagent_max_tool_calls: int,
        subagent_request_timeout: float,
    ) -> None:
        self.hooks = hooks
        self.repair = repair
        self.root = root
        self.run_tool = run_tool
        self.client = client
        self.definitions = definitions
        self.subagent_cls = subagent_cls
        self.mcp_executor = mcp_executor
        self.mcp_remote_tools = mcp_remote_tools
        self.max_delegations = max_delegations
        self.subagent_max_steps = subagent_max_steps
        self.subagent_max_tool_calls = subagent_max_tool_calls
        self.subagent_request_timeout = subagent_request_timeout
        self.delegation_count = 0

    def subagent_executor(self, name: str, args: dict):
        """独立的只读执行入口，不能继承主 Agent 的 allow_write。"""
        readonly_tools = {"list_files", "read_file", "run_tests"}
        if name not in readonly_tools:
            raise PermissionError(f"子 Agent 禁止调用 {name}")

        with self.hooks.guard(name, args, allow_write=False):
            if self.mcp_executor is not None and name in self.mcp_remote_tools:
                return self.mcp_executor(name, args)
            return self.run_tool(name, args, allow_write=False)

    def _delegate(self, args: dict):
        if (
            set(args) != {"task"}
            or not isinstance(args["task"], str)
            or not args["task"].strip()
            or len(args["task"]) > 2000
        ):
            raise ValueError("delegate_test_analysis 需要长度不超过 2000 字符的非空 task")

        if self.delegation_count >= self.max_delegations:
            raise ValueError("本次任务已达到最大子 Agent 委派次数")
        self.delegation_count += 1
        print(f"[Delegation] 第 {self.delegation_count}/{self.max_delegations} 次委派")

        sub_client = (
            self.client.with_options(max_retries=0)
            if callable(getattr(self.client, "with_options", None))
            else self.client
        )
        print("[Sub-agent] 启动 Test Analyst")
        subagent = self.subagent_cls(
            client=sub_client,
            tool_definitions=self.definitions,
            executor=self.subagent_executor,
            model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            max_steps=self.subagent_max_steps,
            max_tool_calls=self.subagent_max_tool_calls,
            request_timeout=self.subagent_request_timeout,
        )
        raw_output = subagent.run(args["task"])
        if raw_output.get("status") != "completed":
            raise RuntimeError(
                "子 Agent 未完成："
                f"{raw_output.get('status', 'unknown')}"
            )
        print("[Sub-agent] 完成：completed")
        return raw_output

    def dispatch(self, name: str, args: dict, *, allow_write: bool):
        """工具必须经过此函数；一个请求只会进入一个执行分支。"""
        if not isinstance(args, dict):
            raise ValueError("工具参数必须是 JSON 对象")

        with self.hooks.guard(name, args, allow_write=allow_write):
            if name == "write_file":
                self.repair.before_write(self.root)

            if name == "delegate_test_analysis":
                return self._delegate(args)
            if self.mcp_executor is not None and name in self.mcp_remote_tools:
                return self.mcp_executor(name, args)
            return self.run_tool(name, args, allow_write=allow_write)
