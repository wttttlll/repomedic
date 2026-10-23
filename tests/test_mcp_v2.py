"""MCP  最小回归测试（不调用 DeepSeek / 不需要启动 MCP Server）。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from integrations.test_runner import run_unittest
from integrations.mcp_bridge import to_deepseek_tools, test_result_to_json


def fake_tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        description=f"fake {name}",
        input_schema={
            "type": "object",
            "properties": (
                {"path": {"type": "string"}} if name == "read_file" else {}
            ),
        },
    )


def fake_result(payload, is_error=False):
    return SimpleNamespace(
        structured_content=payload,
        content=[SimpleNamespace(type="text", text=json.dumps(payload))],
        is_error=is_error,
    )


class MCPV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8",
        )
        (self.repo / "test_calc.py").write_text(
            "import unittest\nfrom calc import add\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_add(self): self.assertEqual(add(2,3),5)\n",
            encoding="utf-8",
        )

    def test_shared_runner_pass(self):
        result = run_unittest(self.repo)
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["test_count"], 1)
        self.assertEqual(result["returncode"], 0)

    def test_shared_runner_fail(self):
        (self.repo / "calc.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8",
        )
        result = run_unittest(self.repo)
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["test_count"], 1)

    def test_zero_tests_is_not_pass(self):
        (self.repo / "test_calc.py").write_text(
            "# no tests\n", encoding="utf-8",
        )
        result = run_unittest(self.repo)
        self.assertFalse(result["passed"])
        self.assertEqual(result["test_count"], 0)

    def test_discovery_ignores_untrusted_extra_tools(self):
        discovered = to_deepseek_tools([
            fake_tool("list_files"),
            fake_tool("read_file"),
            fake_tool("run_tests"),
            fake_tool("delete_everything"),
        ])
        self.assertEqual(
            {entry["function"]["name"] for entry in discovered},
            {"list_files", "read_file", "run_tests"},
        )

    def test_result_schema_rejects_false_pass(self):
        good = {
            "status": "PASS", "passed": True, "returncode": 0,
            "test_count": 1, "output": "Ran 1 test in 0.001s\nOK",
        }
        self.assertEqual(
            json.loads(test_result_to_json(fake_result(good))),
            good,
        )
        self.assertEqual(
            json.loads(test_result_to_json(fake_result({"result": good}))),
            good,
        )
        with self.assertRaises(ValueError):
            test_result_to_json(fake_result({
                "status": "PASS", "passed": True, "returncode": 0,
                "test_count": 0, "output": "No tests",
            }))


if __name__ == "__main__":
    unittest.main(verbosity=2)
