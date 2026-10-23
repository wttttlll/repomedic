# """Deterministic Docker + Verification Controller regression tests.
#
# Never calls DeepSeek or Docker. All code changes occur in temporary files.
# The old test_verification.py can remain for separate real-container E2E tests.
# """
# import copy
# import json
# import tempfile
# import unittest
# from pathlib import Path
# from types import SimpleNamespace
# from unittest.mock import patch
#
# import agent
#
#
# WRONG = "def add(a, b):\\n    return a - b\\n"
# RIGHT = "def add(a, b):\\n    return a + b\\n"
# BAD = "def add(a, b):\\n    return 404\\n"
#
#
# def outcome(status, count=0, code=None, text=""):
#     return {
#         "status": status,
#         "passed": status == "PASS" and code == 0 and count > 0,
#         "returncode": code,
#         "test_count": count,
#         "output": text,
#     }
#
#
# class FakeSessions:
#     def __init__(self):
#         self.history = {"testsess": [{"role": "system", "content": agent.SYSTEM_PROMPT}]}
#
#     def load(self, sid):
#         return copy.deepcopy(self.history[sid])
#
#     def save(self, sid, messages):
#         self.history[sid] = copy.deepcopy(messages)
#
#
# def reply(content="", calls=()):
#     tool_calls = [
#         SimpleNamespace(
#             id=f"call_{n}",
#             function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
#         )
#         for n, (name, args) in enumerate(calls, 1)
#     ]
#     return SimpleNamespace(
#         choices=[SimpleNamespace(
#             finish_reason="tool_calls" if tool_calls else "stop",
#             message=SimpleNamespace(content=content, tool_calls=tool_calls),
#         )],
#         usage=None,
#     )
#
#
# def write(code):
#     return reply(calls=[("write_file", {"path": "calc.py", "content": code})])
#
#
# class FakeClient:
#     def __init__(self, *responses):
#         self.items = iter(responses)
#         self.requests = []
#         self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))
#
#     def create(self, **kwargs):
#         self.requests.append(kwargs)
#         return next(self.items)
#
#
# class VerificationDockerTests(unittest.TestCase):
#     def setUp(self):
#         temp = tempfile.TemporaryDirectory()
#         self.addCleanup(temp.cleanup)
#         self.base = Path(temp.name)
#         self.repo = self.base / "demo_repo"
#         self.repo.mkdir()
#         (self.repo / "calc.py").write_text(WRONG, encoding="utf-8")
#         (self.repo / "test_calc.py").write_text(
#             "import unittest\\nfrom calc import add\\n"
#             "class TestCalc(unittest.TestCase):\\n"
#             "    def test_add(self): self.assertEqual(add(2, 3), 5)\\n",
#             encoding="utf-8",
#         )
#         for obj, attr, value in (
#             (agent, "BASE", self.base),
#             (agent, "ROOT", self.repo),
#             (agent.COMPRESSOR, "build", lambda **kw: kw["messages"]),
#         ):
#             p = patch.object(obj, attr, value)
#             p.start()
#             self.addCleanup(p.stop)
#         self.sessions = FakeSessions()
#
#     def agent_run(self, client, **kwargs):
#         return agent.run_agent(
#             "修复 calc.py", client, self.sessions,
#             "testsess", allow_write=True, **kwargs,
#         )
#
#     def assert_original(self):
#         self.assertEqual((self.repo/"calc.py").read_text(encoding="utf-8"), WRONG)
#
#     def test_execute_tests_uses_shared_runner(self):
#         passed = outcome("PASS", count=2, code=0, text="2 tests OK")
#         with patch.object(agent, "run_unittest", return_value=passed) as runner:
#             self.assertEqual(agent.execute_tests(), passed)
#         runner.assert_called_once_with(self.repo)
#
#     def test_one_write_then_real_evidence(self):
#         client = FakeClient(write(RIGHT), reply("修复完成"))
#         with patch.object(agent, "execute_tests", return_value=outcome("PASS", 2, 0)):
#             answer = self.agent_run(client)
#         self.assertIn("Python 验证成功", answer)
#         self.assertEqual((self.repo/"calc.py").read_text(encoding="utf-8"), RIGHT)
#
#     def test_test_fail_allows_second_fix(self):
#         client = FakeClient(write(BAD), write(RIGHT), reply("完毕"))
#         with patch.object(agent, "execute_tests", side_effect=[
#             outcome("FAIL", 2, 1, "2 assertions failed"),
#             outcome("PASS", 2, 0, "OK"),
#         ]):
#             answer = self.agent_run(client)
#         self.assertIn("共修改 2 次", answer)
#         self.assertEqual((self.repo/"calc.py").read_text(encoding="utf-8"), RIGHT)
#
#     def test_docker_error_rolls_back_without_retrying_code(self):
#         client = FakeClient(write(BAD))
#         with patch.object(agent, "execute_tests", return_value=outcome(
#             "ERROR", 0, 1, "failed to connect to Docker Engine"
#         )):
#             answer = self.agent_run(client)
#         self.assertIn("验证环境异常", answer)
#         self.assertIn("已回滚", answer)
#         self.assertIn("ERROR", answer)
#         self.assertEqual(len(client.requests), 1)
#         self.assert_original()
#
#     def test_docker_timeout_rolls_back(self):
#         client = FakeClient(write(BAD))
#         with patch.object(agent, "execute_tests", return_value=outcome(
#             "TIMEOUT", 0, None, "30s timeout"
#         )):
#             answer = self.agent_run(client)
#         self.assertIn("TIMEOUT", answer)
#         self.assertIn("已回滚", answer)
#         self.assertEqual(len(client.requests), 1)
#         self.assert_original()
#
#     def test_zero_tests_never_claims_pass(self):
#         fake_pass = {"status": "PASS", "passed": True, "returncode": 0,
#                      "test_count": 0, "output": "Ran 0 tests"}
#         client = FakeClient(write(RIGHT))
#         with patch.object(agent, "execute_tests", return_value=fake_pass):
#             answer = self.agent_run(client)
#         self.assertIn("验证环境异常", answer)
#         self.assertNotIn("【Python 验证成功】", answer)
#         self.assert_original()
#
#     def test_error_in_batch_keeps_all_call_ids_paired(self):
#         client = FakeClient(reply(calls=[
#             ("write_file", {"path": "calc.py", "content": BAD}),
#             ("read_file", {"path": "calc.py"}),
#             ("run_tests", {}),
#         ]))
#         with patch.object(agent, "execute_tests", return_value=outcome(
#             "ERROR", 0, 1, "Docker unreachable"
#         )):
#             self.agent_run(client)
#         history = self.sessions.load("testsess")
#         tool_outputs = [m for m in history if m["role"] == "tool"]
#         self.assertEqual(len(tool_outputs), 3)
#         self.assertIn("工具未执行", tool_outputs[1]["content"])
#         self.assertEqual(
#             {m["tool_call_id"] for m in tool_outputs},
#             {"call_1", "call_2", "call_3"},
#         )
#         self.assert_original()
#
#     def test_mcp_retest_error_invalidates_previous_pass(self):
#         client = FakeClient(write(RIGHT), reply(calls=[("run_tests", {})]))
#         mcp_tools = [t for t in agent.TOOLS
#                      if t["function"]["name"] in agent.MCP_REMOTE_TOOLS]
#         seen = []
#         def mcp_executor(name, args):
#             seen.append(name)
#             return json.dumps(outcome("ERROR", 0, 1, "Docker stopped"))
#         with patch.object(agent, "execute_tests", return_value=outcome("PASS", 2, 0)):
#             answer = self.agent_run(
#                 client, mcp_tools=mcp_tools, mcp_executor=mcp_executor
#             )
#         self.assertEqual(seen, ["run_tests"])
#         self.assertIn("验证环境异常", answer)
#         self.assertIn("已回滚", answer)
#         self.assert_original()
#
#     def test_unparseable_mcp_test_result_is_not_success(self):
#         client = FakeClient(write(RIGHT), reply(calls=[("run_tests", {})]))
#         mcp_tools = [t for t in agent.TOOLS
#                      if t["function"]["name"] in agent.MCP_REMOTE_TOOLS]
#         with patch.object(agent, "execute_tests", return_value=outcome("PASS", 2, 0)):
#             answer = self.agent_run(
#                 client, mcp_tools=mcp_tools,
#                 mcp_executor=lambda name, args: "not a JSON test result",
#             )
#         self.assertIn("验证环境异常", answer)
#         self.assert_original()
#
#
# if __name__ == "__main__":
#     unittest.main(verbosity=2)
