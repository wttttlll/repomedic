"""RepoMedic MCP V2 升级器：迁移 run_tests，不修改 CLI 菜单。

预览：python upgrade_mcp_v2.py
安装：python upgrade_mcp_v2.py --apply

要求已整理为 agent.py + integrations/ + tests/。
"""

import argparse
import ast
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TEST_RUNNER_CODE = '"""RepoMedic 固定测试执行器，MCP 和本地 Verification Controller 共用。"""\n\nimport os\nimport re\nimport subprocess\nimport sys\nfrom pathlib import Path\n\n\ndef run_unittest(repo_root: Path) -> dict:\n    """只运行 demo_repo/test_calc.py。该限制不等于安全沙箱。"""\n    root = Path(repo_root).resolve()\n    test_file = root / "test_calc.py"\n    if not test_file.is_file() or test_file.is_symlink():\n        raise ValueError("找不到可信的 test_calc.py")\n\n    allowed_env = ("PATH", "SystemRoot", "WINDIR", "TEMP", "TMP")\n    safe_env = {k: os.environ[k] for k in allowed_env if k in os.environ}\n    safe_env["PYTHONIOENCODING"] = "utf-8"\n\n    try:\n        proc = subprocess.run(\n            [\n                sys.executable, "-I", "-B", "-m", "unittest",\n                "discover", "-s", str(root), "-p", "test_calc.py", "-v",\n            ],\n            cwd=str(root),\n            env=safe_env,\n            stdin=subprocess.DEVNULL,\n            capture_output=True,\n            text=True,\n            encoding="utf-8",\n            errors="replace",\n            timeout=10,\n        )\n    except subprocess.TimeoutExpired:\n        return {\n            "status": "TIMEOUT",\n            "passed": False,\n            "returncode": None,\n            "test_count": 0,\n            "output": "测试超时：超过 10 秒",\n        }\n\n    output = ((proc.stdout or "") + "\\n" + (proc.stderr or "")).strip()\n    matched = re.search(r"\\bRan\\s+(\\d+)\\s+tests?\\s+in\\b", output)\n    count = int(matched.group(1)) if matched else 0\n\n    passed = proc.returncode == 0 and count > 0\n    return {\n        "status": "PASS" if passed else "FAIL",\n        "passed": passed,\n        "returncode": proc.returncode,\n        "test_count": count,\n        "output": output[:6000],\n    }\n'
MCP_TEST_CODE = '"""MCP V2 最小回归测试（不调用 DeepSeek / 不需要启动 MCP Server）。"""\n\nimport json\nimport tempfile\nimport unittest\nfrom pathlib import Path\nfrom types import SimpleNamespace\n\nfrom integrations.test_runner import run_unittest\nfrom integrations.mcp_bridge import to_deepseek_tools, test_result_to_json\n\n\ndef fake_tool(name: str) -> SimpleNamespace:\n    return SimpleNamespace(\n        name=name,\n        description=f"fake {name}",\n        input_schema={\n            "type": "object",\n            "properties": (\n                {"path": {"type": "string"}} if name == "read_file" else {}\n            ),\n        },\n    )\n\n\ndef fake_result(payload, is_error=False):\n    return SimpleNamespace(\n        structured_content=payload,\n        content=[SimpleNamespace(type="text", text=json.dumps(payload))],\n        is_error=is_error,\n    )\n\n\nclass MCPV2Tests(unittest.TestCase):\n    def setUp(self):\n        self.temp = tempfile.TemporaryDirectory()\n        self.addCleanup(self.temp.cleanup)\n        self.repo = Path(self.temp.name)\n        (self.repo / "calc.py").write_text(\n            "def add(a, b):\\n    return a + b\\n", encoding="utf-8",\n        )\n        (self.repo / "test_calc.py").write_text(\n            "import unittest\\nfrom calc import add\\n"\n            "class TestCalc(unittest.TestCase):\\n"\n            "    def test_add(self): self.assertEqual(add(2,3),5)\\n",\n            encoding="utf-8",\n        )\n\n    def test_shared_runner_pass(self):\n        result = run_unittest(self.repo)\n        self.assertTrue(result["passed"], result)\n        self.assertEqual(result["test_count"], 1)\n        self.assertEqual(result["returncode"], 0)\n\n    def test_shared_runner_fail(self):\n        (self.repo / "calc.py").write_text(\n            "def add(a, b):\\n    return a - b\\n", encoding="utf-8",\n        )\n        result = run_unittest(self.repo)\n        self.assertFalse(result["passed"])\n        self.assertEqual(result["status"], "FAIL")\n        self.assertEqual(result["test_count"], 1)\n\n    def test_zero_tests_is_not_pass(self):\n        (self.repo / "test_calc.py").write_text(\n            "# no tests\\n", encoding="utf-8",\n        )\n        result = run_unittest(self.repo)\n        self.assertFalse(result["passed"])\n        self.assertEqual(result["test_count"], 0)\n\n    def test_discovery_ignores_untrusted_extra_tools(self):\n        discovered = to_deepseek_tools([\n            fake_tool("list_files"),\n            fake_tool("read_file"),\n            fake_tool("run_tests"),\n            fake_tool("delete_everything"),\n        ])\n        self.assertEqual(\n            {entry["function"]["name"] for entry in discovered},\n            {"list_files", "read_file", "run_tests"},\n        )\n\n    def test_result_schema_rejects_false_pass(self):\n        good = {\n            "status": "PASS", "passed": True, "returncode": 0,\n            "test_count": 1, "output": "Ran 1 test in 0.001s\\nOK",\n        }\n        self.assertEqual(\n            json.loads(test_result_to_json(fake_result(good))),\n            good,\n        )\n        self.assertEqual(\n            json.loads(test_result_to_json(fake_result({"result": good}))),\n            good,\n        )\n        with self.assertRaises(ValueError):\n            test_result_to_json(fake_result({\n                "status": "PASS", "passed": True, "returncode": 0,\n                "test_count": 0, "output": "No tests",\n            }))\n\n\nif __name__ == "__main__":\n    unittest.main(verbosity=2)\n'


class UpgradeError(Exception):
    pass


def require(code: str, marker: str, filename: str) -> None:
    if marker not in code:
        raise UpgradeError(
            f"无法在 {filename} 中找到预期代码片段：{marker!r}；"
            "为避免覆盖自定义代码，本次操作停止。"
        )


def replace_once(code: str, old: str, new: str, filename: str) -> str:
    require(code, old, filename)
    return code.replace(old, new, 1)


def replace_top_level_function(
    code: str, name: str, replacement: str,
) -> str:
    tree = ast.parse(code)
    functions = [
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    if len(functions) != 1:
        raise UpgradeError(f"未找到唯一的顶层函数 {name}。")
    function = functions[0]
    lines = code.splitlines(keepends=True)
    return (
        "".join(lines[:function.lineno - 1])
        + replacement.rstrip() + "\n\n"
        + "".join(lines[function.end_lineno:])
    )


def patch_agent(code: str) -> str:
    name = "agent.py"
    require(code, "from integrations.mcp_bridge import", name)
    require(code, "def execute_tests() -> dict:", name)

    code = replace_once(
        code,
        'MCP_READ_TOOLS = frozenset({"list_files", "read_file"})',
        'MCP_REMOTE_TOOLS = frozenset({"list_files", "read_file", "run_tests"})',
        name,
    )
    code = code.replace("MCP_READ_TOOLS", "MCP_REMOTE_TOOLS")

    code = replace_once(
        code,
        "from context.context_compressor import ContextCompressor\n",
        "from context.context_compressor import ContextCompressor\n"
        "from integrations.test_runner import run_unittest\n",
        name,
    )

    code = replace_top_level_function(
        code,
        "execute_tests",
        (
            "def execute_tests() -> dict:\n"
            '    """由本地 Verification Controller 独立执行最终测试。"""\n'
            "    return run_unittest(ROOT)\n"
        ),
    )

    # 模型调用 MCP run_tests 不允许改变本地自动验证的通过判定。
    code = replace_once(
        code,
        'elif name == "run_tests" and wrote_file:',
        'elif name == "run_tests" and wrote_file and mcp_executor is None:',
        name,
    )

    return code


def patch_server(code: str) -> str:
    name = "integrations/mcp_server.py"
    require(code, "from mcp.server import MCPServer", name)
    require(code, "def list_files()", name)
    require(code, "def read_file(", name)
    require(code, 'if __name__ == "__main__":', name)

    code = replace_once(
        code, "from pathlib import Path\n",
        "from pathlib import Path\nfrom typing import TypedDict\n",
        name,
    )
    sibling_import = (
        "\nif __package__:\n"
        "    from .test_runner import run_unittest\n"
        "else:\n"
        "    from test_runner import run_unittest\n"
    )
    code = replace_once(
        code,
        "from mcp.server.mcpserver.exceptions import ToolError\n",
        "from mcp.server.mcpserver.exceptions import ToolError\n" + sibling_import,
        name,
    )

    extension = """class TestResult(TypedDict):
    status: str
    passed: bool
    returncode: int | None
    test_count: int
    output: str


@mcp.tool()
def run_tests() -> TestResult:
    \"\"\"运行 demo_repo 的固定单元测试，返回测试状态、退出码和日志；不接受命令参数。\"\"\"
    return run_unittest(ROOT)


"""

    code = code.replace(
        "只能读取 demo_repo 内的文件。",
        "可以读取 demo_repo 文件并运行固定单元测试；禁止写入。",
    )
    return replace_once(
        code,
        'if __name__ == "__main__":',
        extension + 'if __name__ == "__main__":',
        name,
    )


TEST_RESULT_HELPER = """def test_result_to_json(result) -> str:
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


"""


def patch_bridge(code: str) -> str:
    name = "integrations/mcp_bridge.py"
    require(code, "def mcp_result_as_text(", name)
    require(code, "async def run_agent_with_mcp(", name)

    code = replace_once(
        code,
        'MCP_READ_TOOLS = frozenset({"list_files", "read_file"})',
        'MCP_REMOTE_TOOLS = frozenset({"list_files", "read_file", "run_tests"})',
        name,
    )
    code = code.replace("MCP_READ_TOOLS", "MCP_REMOTE_TOOLS")
    code = code.replace("必须提供两个只读工具", "必须提供三个工具")

    code = replace_once(
        code,
        "async def run_agent_with_mcp(",
        TEST_RESULT_HELPER + "async def run_agent_with_mcp(",
        name,
    )

    code = replace_once(
        code,
        "            return text\n\n        return await asyncio.to_thread(",
        '            if name == "run_tests":\n'
        "                return test_result_to_json(result)\n\n"
        "            return text\n\n"
        "        return await asyncio.to_thread(",
        name,
    )
    return code


def patch_client(code: str) -> str:
    name = "integrations/mcp_client.py"
    marker = '{"list_files", "read_file"}.issubset(names)'
    if marker not in code:
        print("[NOTE] MCP Client 是自定义版本，保留原代码不修改。")
        return code

    code = replace_once(
        code,
        marker,
        '{"list_files", "read_file", "run_tests"}.issubset(names)',
        name,
    )
    before = "        # Negative test: reject path traversal"
    inserted = (
        '        tests = await client.call_tool("run_tests", {})\n'
        '        show_result("run_tests", tests)\n'
        '        if tests.is_error:\n'
        '            raise RuntimeError("MCP run_tests 调用失败")\n\n'
    )
    return replace_once(code, before, inserted + before, name)



def harden_existing_v2_agent(code: str) -> str:
    # 旧 V2 的远程 run_tests 可能覆盖本地 Verification 状态。
    unsafe = 'elif name == "run_tests" and wrote_file:'
    if unsafe in code:
        return code.replace(
            unsafe,
            'elif name == "run_tests" and wrote_file and mcp_executor is None:',
            1,
        )
    return code


def harden_existing_v2_bridge(code: str) -> str:
    # 补上对 MCP 测试结果的结构校验，保持已有连接逻辑不变。
    if "def test_result_to_json(" in code:
        return code

    old = """            # MCP 的 run_tests 返回 TypedDict，优先使用结构化测试数据。
            # list_files / read_file 继续使用 SDK 返回的文本表示。
            if name == "run_tests":
                structured = getattr(result, "structured_content", None)
                if isinstance(structured, dict):
                    test_data = structured.get("result", structured)
                    if isinstance(test_data, dict):
                        return json.dumps(test_data, ensure_ascii=False)

            return text"""

    new = """            if name == "run_tests":
                return test_result_to_json(result)

            return text"""

    require(code, old, "../integrations/mcp_bridge.py")
    code = code.replace(old, new, 1)
    return replace_once(
        code,
        "async def run_agent_with_mcp(",
        TEST_RESULT_HELPER + "async def run_agent_with_mcp(",
        "../integrations/mcp_bridge.py",
    )

def detect(agent: str, server: str, bridge: str) -> str:
    sentinel = 'MCP_REMOTE_TOOLS = frozenset({"list_files", "read_file", "run_tests"})'
    flags = (
        sentinel in agent,
        "def run_tests()" in server,
        sentinel in bridge,
    )
    if all(flags):
        return "already_v2"
    if any(flags):
        raise UpgradeError(
            "检测到部分模块已升级到 MCP V2，存在混合版本。"
            "为了防止覆盖现有代码，请先检查 agent.py、mcp_server.py、mcp_bridge.py。"
        )
    return "v1"


def build_plan() -> tuple[str, dict[Path, str]]:
    agent = ROOT / "agent.py"
    folder = ROOT / "integrations"
    server = folder / "mcp_server.py"
    bridge = folder / "mcp_bridge.py"
    client = folder / "mcp_client.py"
    runner = folder / "test_runner.py"
    tests = ROOT / "tests" / "test_mcp_v2.py"

    for path in (agent, server, bridge):
        if not path.is_file():
            raise UpgradeError(
                f"缺少 {path.relative_to(ROOT)}。请先确认项目目录已整理。"
            )

    originals = {
        agent: agent.read_text(encoding="utf-8"),
        server: server.read_text(encoding="utf-8"),
        bridge: bridge.read_text(encoding="utf-8"),
    }

    stage = detect(originals[agent], originals[server], originals[bridge])
    if stage == "already_v2":
        if not runner.is_file():
            raise UpgradeError("已有 MCP V2 配置，但找不到 integrations/test_runner.py")

        changes = {}
        upgraded_agent = harden_existing_v2_agent(originals[agent])
        upgraded_bridge = harden_existing_v2_bridge(originals[bridge])
        if upgraded_agent != originals[agent]:
            changes[agent] = upgraded_agent
        if upgraded_bridge != originals[bridge]:
            changes[bridge] = upgraded_bridge
        if not tests.exists():
            changes[tests] = MCP_TEST_CODE

        for path, text in changes.items():
            ast.parse(text, filename=str(path))
        return ("v2_hardening" if changes else "already_v2"), changes

    if runner.exists():
        raise UpgradeError(
            "发现 integrations/test_runner.py，但 MCP 尚未完全升级。"
            "为了不覆盖代码，请先核对现有文件。"
        )
    if tests.exists():
        raise UpgradeError("tests/test_mcp_v2.py 已存在，不会覆盖原测试。")

    changes = {
        agent: patch_agent(originals[agent]),
        server: patch_server(originals[server]),
        bridge: patch_bridge(originals[bridge]),
        runner: TEST_RUNNER_CODE,
        tests: MCP_TEST_CODE,
    }
    if client.is_file():
        changes[client] = patch_client(client.read_text(encoding="utf-8"))

    for path, text in changes.items():
        ast.parse(text, filename=str(path))
    return stage, changes


def make_backup(changes: dict[Path, str]) -> Path:
    name = f"{ROOT.name}_mcp_v2_backup_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}"
    folder = ROOT.parent / name
    folder.mkdir(parents=True, exist_ok=False)
    for path in changes:
        if path.exists():
            dest = folder / path.relative_to(ROOT)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    return folder


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(text, encoding="utf-8")
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="RepoMedic MCP V2 代码升级器")
    parser.add_argument("--apply", action="store_true", help="备份后正式升级")
    args = parser.parse_args()

    try:
        stage, changes = build_plan()
    except (UpgradeError, SyntaxError, OSError, ValueError) as exc:
        print(f"[STOP] {exc}", file=sys.stderr)
        return 1

    if stage == "already_v2":
        print("[OK] MCP run_tests 已在 Agent / Bridge / Server 中配置。")
        print("请运行 python -m integrations.mcp_client 做实际验证。")
        return 0

    print("\nRepoMedic MCP V2 升级清单")
    print("=" * 55)
    for path, text in changes.items():
        status = "创建" if not path.exists() else "修改"
        if path.exists() and path.read_text(encoding="utf-8") == text:
            status = "无变化"
        print(f"  [{status}] {path.relative_to(ROOT)}")
    print("  原有 CLI 菜单 / Session / Context / demo_repo 完全保留")

    if not args.apply:
        print("\n[DRY RUN] 没有修改任何文件。")
        print("确定无误后执行：python upgrade_mcp_v2.py --apply")
        return 0

    backup = make_backup(changes)
    print(f"\n[BACKUP] 原代码备份：{backup}")
    try:
        for path, text in changes.items():
            atomic_write(path, text)
            print(f"[WRITE] {path.relative_to(ROOT)}")
    except Exception as exc:
        print(f"[ERROR] 写入失败：{exc}", file=sys.stderr)
        failed_restores = []
        for path in changes:
            saved = backup / path.relative_to(ROOT)
            try:
                if saved.is_file():
                    shutil.copy2(saved, path)
                else:
                    path.unlink(missing_ok=True)
            except OSError as restore_exc:
                failed_restores.append((str(path.relative_to(ROOT)), str(restore_exc)))

        if failed_restores:
            print(f"[RECOVER] 有文件未能自动恢复：{failed_restores}", file=sys.stderr)
            print(f"[BACKUP] 请使用原文件备份手动恢复：{backup}")
        else:
            print("[ROLLBACK] 升级未完成，原代码已自动恢复。")
        return 1

    print("\n[OK] 代码升级完成。请按顺序验证：")
    print('  python -m unittest discover -s tests -p "test_*.py" -v')
    print("  python -m integrations.mcp_client")
    print("  python agent.py")
    print("重要：固定 unittest 不是隔离沙箱，只执行可信测试代码。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
