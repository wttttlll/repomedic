
import asyncio
import sys
from pathlib import Path

from mcp import Client, StdioServerParameters


async def main():
    server_file = Path(__file__).resolve().with_name("mcp_server.py")

    print("正在启动 MCP Server...", flush=True)

    params = StdioServerParameters(
        command=sys.executable,
        args=[str(server_file)],
    )

    async with Client(params) as client:
        print("MCP 连接成功！")

        # 1. 发现工具
        result = await client.list_tools()
        names = [tool.name for tool in result.tools]
        print("发现的工具：", names)

        # 2. 调用 list_files
        result = await client.call_tool("list_files", {})
        print("list_files 结果：")
        for item in result.content:
            if hasattr(item, "text"):
                print(item.text)

        # 3. 调用 read_file
        result = await client.call_tool(
            "read_file",
            {"path": "calc.py"},
        )
        print("read_file 结果：")
        for item in result.content:
            if hasattr(item, "text"):
                print(item.text)

        # 4. 检查路径越界是否被阻止
        result = await client.call_tool(
            "read_file",
            {"path": "../agent.py"},
        )
        print("路径越界被拒绝：", result.is_error)

    print("MCP 测试结束！")


if __name__ == "__main__":
    asyncio.run(main())