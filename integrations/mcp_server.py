"""RepoMedic MCP Server v0: read-only tools using local stdio."""
from pathlib import Path
from typing import TypedDict

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

if __package__:
    from .test_runner import run_unittest
else:
    from test_runner import run_unittest

ROOT = (Path(__file__).resolve().parent.parent / "demo_repo").resolve()
MAX_FILE_BYTES = 32 * 1024
mcp = MCPServer("RepoMedic Files", instructions="可以读取 demo_repo 文件并运行固定单元测试；禁止写入。")


@mcp.tool()
def list_files() -> list[str]:
    """列出 demo_repo 根目录中的文件和文件夹。"""
    if not ROOT.is_dir():
        raise ToolError("demo_repo 文件夹不存在")
    return sorted(
        p.name + ("/" if p.is_dir() else "")
        for p in ROOT.iterdir()
        if not p.is_symlink()
    )


@mcp.tool()
def read_file(path: str) -> str:
    """读取 demo_repo 内不超过 32 KiB 的 UTF-8 文本文件。"""
    relative = Path(path)
    if relative.is_absolute():
        raise ToolError("禁止使用绝对路径")

    candidate = ROOT / relative
    target = candidate.resolve()
    if not target.is_relative_to(ROOT):
        raise ToolError("禁止读取 demo_repo 之外的文件")
    if candidate.is_symlink():
        raise ToolError("禁止读取符号链接")
    if not target.is_file():
        raise ToolError(f"文件不存在: {path}")
    if target.stat().st_size > MAX_FILE_BYTES:
        raise ToolError("文件超过 32 KiB")

    try:
        return target.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError("只支持 UTF-8 文本文件") from exc


class TestResult(TypedDict):
    status: str
    passed: bool
    returncode: int | None
    test_count: int
    output: str


@mcp.tool()
def run_tests() -> TestResult:
    """运行 demo_repo 的固定单元测试，返回测试状态、退出码和日志；不接受命令参数。"""
    return run_unittest(ROOT)


if __name__ == "__main__":
    mcp.run()
