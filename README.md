# repomedic
代码修复mini-agent

## 项目结构
- `benchmarks/` - 性能基准测试
- `context/` - 上下文管理
- `core/` - 核心执行逻辑（Agent Loop、验证、共享测试入口）
- `data/` - 数据存储
- `docs/` - 项目文档
- `hooks/` - 钩子管理
- `integrations/` - 外部集成（PPT 等）
- `repomedic_mcp/` - 独立 MCP 包（Server / Bridge / Client）
- `sandbox/` - 安全代码执行
- `sessions/` - 会话管理
- `skills/` - 智能体技能
- `subagents/` - 多智能体协作
- `tests/` - 自动化测试

## MCP

MCP 协议与传输层独立在 `repomedic_mcp/`，核心业务仍在 `core/`。
详细边界见 [docs/MCP_PACKAGE.md](docs/MCP_PACKAGE.md)。

```bash
# 启动 stdio MCP Server（也可用 python repomedic_mcp/server.py）
python -m repomedic_mcp

# 连接本机 Server 并发现/调用工具
python -m repomedic_mcp.client

# 运行 MCP 包测试（进程内 transport，不需要 Docker）
python -m pytest tests/test_mcp_package.py -q
```

MCP 相关实现统一位于 `repomedic_mcp/`，共享测试入口为 `core.test_runner`。
`integrations/` 不再包含 MCP 兼容层，旧路径 `integrations.mcp_*` 与
`integrations.test_runner` 已移除。

## Agent 状态与 Resume

运行状态、Checkpoint 与工具执行日志保存在 `data/state_data/agent_state.db`。
每次运行拥有独立 `run_id`，中断后可用同一个 `run_id` 继续。

```python
from agent import (
    abandon_resume,
    confirm_tool_executed,
    confirm_tool_not_executed,
    resume_agent,
)

# 使用原 Run 的 run_id、同一个 client / sessions 继续执行
answer = resume_agent(run_id, client, sessions)

# WAITING_APPROVAL：人工确认工具已执行，并补记真实结果
confirm_tool_executed(run_id, call_id, result, verified=True)

# 或确认工具未执行，允许 Resume 安全重试
# write_file 可用 expected_content 校验当前工作区
confirm_tool_not_executed(
    run_id, call_id, expected_content=original_content
)

# 放弃恢复：不改动工作区，保留审计数据
abandon_resume(run_id, "人工确认放弃")
```

- `write_file` 会校验目标文件内容、备份路径和验证结果；文件不匹配时拒绝恢复。
- `generate_ppt`、MCP 写工具、Shell/未知工具无法可靠核对副作用，默认阻止自动
  重试，必须显式传入 `verified=True` 才能人工补记或批准重试。
- 已完成/失败 Run 默认最多保留 5 个 Checkpoint；未完成 Run 的恢复数据不会被清理。
