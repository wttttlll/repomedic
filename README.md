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
