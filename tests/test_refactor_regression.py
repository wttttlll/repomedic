"""模块化重构专用回归测试：全部模拟 LLM，不请求真实 DeepSeek 或 Docker。

运行：python -m unittest discover -s tests -p "test_refactor_regression.py" -v
"""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent


WRONG = "def add(a, b):\n    return a - b\n"
CORRECT = "def add(a, b):\n    return a + b\n"
BAD = "def add(a, b):\n    return 404\n"


def status(name):
    if name == "PASS":
        return {"status": "PASS", "passed": True, "returncode": 0,
                "test_count": 2, "output": "Ran 2 tests - OK"}
    return {"status": name, "passed": False,
            "returncode": 1 if name == "FAIL" else None,
            "test_count": 2 if name == "FAIL" else 0,
            "output": f"Ran 2 tests - {name}"}


def reply(*tool_requests, text="完成"):
    calls = [
        SimpleNamespace(
            id=f"call_{i}",
            function=SimpleNamespace(
                name=name, arguments=json.dumps(args, ensure_ascii=False),
            ),
        )
        for i, (name, args) in enumerate(tool_requests, 1)
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="tool_calls" if calls else "stop",
            message=SimpleNamespace(
                content=None if calls else text, tool_calls=calls,
            ),
        )],
        usage=None,
    )


class FakeClient:
    def __init__(self, *responses):
        self._responses = iter(responses)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = next(self._responses)
        if isinstance(item, Exception):
            raise item
        return item


class FakeSessions:
    def __init__(self):
        self.data = [{"role": "system", "content": agent.SYSTEM_PROMPT}]

    def load(self, session_id):
        return copy.deepcopy(self.data)

    def save(self, session_id, messages):
        self.data = copy.deepcopy(messages)


class RefactorRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "demo_repo"
        self.root.mkdir()
        (self.root / "calc.py").write_text(WRONG, encoding="utf-8")
        (self.root / "test_calc.py").write_text("# tests are mocked", encoding="utf-8")

        for obj, attr, value in (
            (agent, "ROOT", self.root),
            (agent, "BASE", self.base),
            (agent.COMPRESSOR,
             "build", lambda **kwargs: kwargs["messages"]),
            (agent.SKILL_LOADER, "select", lambda task: None),
        ):
            p = patch.object(obj, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.sessions = FakeSessions()

    def run_agent(self, *responses, allow_write=False):
        client = FakeClient(*responses)
        result = agent.run_agent(
            "诊断和修复 calc.py", client, self.sessions,
            "test", allow_write=allow_write,
        )
        return result, client

    def tools(self):
        return [m for m in self.sessions.data if m["role"] == "tool"]

    def test_readonly_hides_write_tool(self):
        answer, client = self.run_agent(reply(text="检查结束"))
        names = [t["function"]["name"] for t in client.requests[0]["tools"]]
        self.assertNotIn("write_file", names)
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"), WRONG)
        self.assertIn("检查结束", answer)

    def test_readonly_denies_write_even_if_model_forges_tool_call(self):
        self.run_agent(
            reply(("write_file", {"path": "calc.py", "content": CORRECT})),
            reply(text="结束"),
        )
        self.assertIn("工具错误", self.tools()[0]["content"])
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"), WRONG)

    def test_batch_tool_calls_keep_ids_even_when_one_fails(self):
        with patch.object(agent, "execute_tests", return_value=status("FAIL")):
            self.run_agent(
                reply(
                    ("list_files", {}),
                    ("read_file", {"path": "missing.py"}),
                    ("run_tests", {}),
                ),
                reply(text="报告完成"),
            )
        tools = self.tools()
        self.assertEqual(len(tools), 3)
        self.assertEqual([x["tool_call_id"] for x in tools], ["call_1", "call_2", "call_3"])
        self.assertIn("工具错误", tools[1]["content"])
        self.assertIn("FAIL", tools[2]["content"])

    def test_first_write_can_pass_real_runtime_state(self):
        with patch.object(agent, "execute_tests", return_value=status("PASS")):
            result, _ = self.run_agent(
                reply(("write_file", {"path": "calc.py", "content": CORRECT})),
                reply(text="修复完成"),
                allow_write=True,
            )
        self.assertIn("Python 验证成功", result)
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"), CORRECT)
        self.assertTrue(list((self.base / "data" / "backups").glob("*.bak")))

    def test_three_failures_rollback_and_pair_all_batch_calls(self):
        batch = [("write_file", {"path": "calc.py", "content": BAD}) for _ in range(4)]
        with patch.object(agent, "execute_tests", return_value=status("FAIL")):
            result, _ = self.run_agent(reply(*batch), allow_write=True)
        self.assertIn("自动修复失败", result)
        self.assertIn("已回滚", result)
        self.assertEqual(len(self.tools()), 4)
        self.assertIn("工具未执行", self.tools()[-1]["content"])
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"), WRONG)

    def test_early_false_claim_prompts_second_repair(self):
        with patch.object(agent, "execute_tests", side_effect=[status("FAIL"), status("PASS")]):
            result, client = self.run_agent(
                reply(("write_file", {"path": "calc.py", "content": BAD})),
                reply(text="已经修好了"),
                reply(("write_file", {"path": "calc.py", "content": CORRECT})),
                reply(text="完成"),
                allow_write=True,
            )
        self.assertIn("Python 验证成功", result)
        self.assertEqual(len(client.requests), 4)

    def test_docker_error_rolls_back(self):
        with patch.object(agent, "execute_tests", return_value=status("ERROR")):
            result, _ = self.run_agent(
                reply(("write_file", {"path": "calc.py", "content": BAD})),
                allow_write=True,
            )
        self.assertIn("验证环境异常", result)
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"), WRONG)

    def test_docker_timeout_rolls_back(self):
        with patch.object(agent, "execute_tests", return_value=status("TIMEOUT")):
            result, _ = self.run_agent(
                reply(("write_file", {"path": "calc.py", "content": BAD})),
                allow_write=True,
            )
        self.assertIn("验证环境异常", result)
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"), WRONG)

    def test_api_exception_rolls_back(self):
        with patch.object(agent, "execute_tests", return_value=status("FAIL")):
            with self.assertRaisesRegex(RuntimeError, "模拟的 API 异常"):
                self.run_agent(
                    reply(("write_file", {"path": "calc.py", "content": BAD})),
                    RuntimeError("模拟的 API 异常"),
                    allow_write=True,
                )
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"), WRONG)

    def test_reject_test_file_write(self):
        with self.assertRaises(PermissionError):
            agent.run_tool(
                "write_file",
                {"path": "test_calc.py", "content": "malicious"},
                allow_write=True,
            )
        self.assertEqual((self.root / "test_calc.py").read_text(), "# tests are mocked")

    def test_suspicious_pass_does_not_count(self):
        bad = {"status": "PASS", "passed": True, "returncode": 0,
               "test_count": 0, "output": "no tests"}
        self.assertEqual(agent._checked_test_result(bad)["status"], "ERROR")

    def test_subagent_is_only_dispatched_twice(self):
        instances = []

        class FakeChild:
            def __init__(self, **kwargs):
                instances.append(self)
                self.kwargs = kwargs

            def run(self, task):
                return {"status": "completed", "analysis": "分析完成", "used_tools": []}

        with patch.object(agent, "TestAnalystSubAgent", FakeChild):
            self.run_agent(
                reply(*[("delegate_test_analysis", {"task": "检查代码"}) for _ in range(3)]),
                reply(text="汇总完成"),
            )
        self.assertEqual(len(instances), 2)
        self.assertEqual(len(self.tools()), 3)
        self.assertIn("达到最大子 Agent 委派次数", self.tools()[2]["content"])
        self.assertEqual(instances[0].kwargs["request_timeout"], 20.0)

    def test_subagent_noncompleted_returns_error_not_success(self):
        class FakeChild:
            def __init__(self, **kwargs):
                pass

            def run(self, task):
                return {"status": "max_steps", "analysis": ""}

        with patch.object(agent, "TestAnalystSubAgent", FakeChild):
            self.run_agent(
                reply(("delegate_test_analysis", {"task": "检查代码"})),
                reply(text="收到未完成反馈"),
            )
        self.assertIn("子 Agent 未完成：max_steps", self.tools()[0]["content"])

    def test_mcp_calls_route_once_to_remote_executor(self):
        observed = []

        def mcp_executor(name, args):
            observed.append((name, args))
            return "def add(a,b): return a-b"

        remote = [
            item for item in agent.TOOLS
            if item["function"]["name"] in agent.MCP_REMOTE_TOOLS
        ]
        with patch.object(agent, "run_tool", side_effect=AssertionError("不能调用本地工具")):
            agent.run_agent(
                "查看文件", FakeClient(
                    reply(("read_file", {"path": "calc.py"})),
                    reply(text="报告完成"),
                ), self.sessions, "test",
                allow_write=False, mcp_tools=remote, mcp_executor=mcp_executor,
            )
        self.assertEqual(observed, [("read_file", {"path": "calc.py"})])
        self.assertIn("return a-b", self.tools()[0]["content"])

    def test_subagent_cannot_gain_write_permission_from_main_agent(self):
        observed = []

        class FakeChild:
            def __init__(self, **kwargs):
                self.executor = kwargs["executor"]

            def run(self, task):
                observed.append(self.executor("read_file", {"path": "calc.py"}))
                try:
                    self.executor("write_file", {"path": "calc.py", "content": BAD})
                except PermissionError:
                    observed.append("write denied")
                else:
                    raise AssertionError("子 Agent 错误获得写权限")
                return {"status": "completed", "analysis": "已分析", "used_tools": ["read_file"]}

        remote = [
            item for item in agent.TOOLS
            if item["function"]["name"] in agent.MCP_REMOTE_TOOLS
        ]
        with patch.object(agent, "TestAnalystSubAgent", FakeChild):
            agent.run_agent(
                "委派只读任务", FakeClient(
                    reply(("delegate_test_analysis", {"task": "检查代码"})),
                    reply(text="完成"),
                ), self.sessions, "test", allow_write=True,
                mcp_tools=remote,
                mcp_executor=lambda name, args: "MCP read successful",
            )
        self.assertEqual(observed, ["MCP read successful", "write denied"])
        self.assertEqual((self.root / "calc.py").read_text(encoding="utf-8"), WRONG)

    def test_untrusted_path_is_not_readable(self):
        with self.assertRaises(ValueError):
            agent.run_tool("read_file", {"path": "../secret.py"})


if __name__ == "__main__":
    unittest.main()
