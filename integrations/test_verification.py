"""RepoMedic Verification Loop V2 的确定性回归测试。

放在 agent.py 同级目录运行：
    python -m unittest test_verification -v

不访问 DeepSeek；模拟模型工具调用，但真实运行 unittest 子进程。
只修改临时目录，不会更改你的 demo_repo。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent


WRONG_CODE = "def add(a, b):\n    return a - b\n"
CORRECT_CODE = "def add(a, b):\n    return a + b\n"
STILL_WRONG_CODE = "def add(a, b):\n    return 404\n"


class FakeSessions:
    def __init__(self):
        self.history = {
            "testsession": [{"role": "system", "content": agent.SYSTEM_PROMPT}]
        }

    def load(self, session_id):
        return copy.deepcopy(self.history[session_id])

    def save(self, session_id, messages):
        self.history[session_id] = copy.deepcopy(messages)


def reply(content="", tool_requests=()):
    calls = [
        SimpleNamespace(
            id=f"call_{idx}",
            function=SimpleNamespace(
                name=name,
                arguments=json.dumps(args, ensure_ascii=False),
            ),
        )
        for idx, (name, args) in enumerate(tool_requests, 1)
    ]
    return SimpleNamespace(
        choices=[SimpleNamespace(
            finish_reason="tool_calls" if calls else "stop",
            message=SimpleNamespace(content=content, tool_calls=calls),
        )],
        usage=None,
    )


def write_response(code):
    return reply(tool_requests=[
        ("write_file", {"path": "calc.py", "content": code})
    ])


class FakeClient:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.requests = []
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self.create)
        )

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = next(self.responses)
        if isinstance(item, Exception):
            raise item
        return item


class VerificationV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

        self.base = Path(self.temp.name)
        self.root = self.base / "demo_repo"
        self.root.mkdir()
        (self.root / "calc.py").write_text(WRONG_CODE, encoding="utf-8")
        (self.root / "test_calc.py").write_text(
            "import unittest\n"
            "from calc import add\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_add_positive(self): self.assertEqual(add(2, 3), 5)\n"
            "    def test_add_zero(self): self.assertEqual(add(-1, 1), 0)\n",
            encoding="utf-8",
        )

        for obj, attribute, replacement in (
            (agent, "BASE", self.base),
            (agent, "ROOT", self.root),
            (
                agent.COMPRESSOR, "build",
                lambda **kwargs: kwargs["messages"],
            ),
        ):
            patcher = patch.object(obj, attribute, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.sessions = FakeSessions()
        self.sid = "testsession"

    def run_agent(self, client, write=True):
        return agent.run_agent(
            "修复 calc.py", client, self.sessions,
            self.sid, allow_write=write,
        )

    def test_readonly_hides_write_tool(self):
        client = FakeClient(reply("检查完成"))
        self.run_agent(client, write=False)
        names = [
            entry["function"]["name"]
            for entry in client.requests[0]["tools"]
        ]
        self.assertNotIn("write_file", names)
        self.assertEqual(
            (self.root / "calc.py").read_text(encoding="utf-8"),
            WRONG_CODE,
        )

    def test_one_attempt_passes(self):
        client = FakeClient(
            write_response(CORRECT_CODE),
            reply("修复成功"),
        )
        answer = self.run_agent(client)
        self.assertIn("Python 验证成功", answer)
        self.assertEqual(
            (self.root / "calc.py").read_text(encoding="utf-8"),
            CORRECT_CODE,
        )
        self.assertTrue(list((self.base / "backups").glob("*.bak")))

    def test_second_attempt_recovers(self):
        client = FakeClient(
            write_response(STILL_WRONG_CODE),
            write_response(CORRECT_CODE),
            reply("已经完成"),
        )
        answer = self.run_agent(client)
        self.assertIn("共修改 2 次", answer)
        self.assertEqual(
            (self.root / "calc.py").read_text(encoding="utf-8"),
            CORRECT_CODE,
        )

    def test_three_failures_roll_back(self):
        client = FakeClient(*[
            write_response(STILL_WRONG_CODE)
            for _ in range(3)
        ])
        answer = self.run_agent(client)
        self.assertIn("自动修复失败", answer)
        self.assertIn("已回滚", answer)
        self.assertEqual(
            (self.root / "calc.py").read_text(encoding="utf-8"),
            WRONG_CODE,
        )

    def test_early_false_success_triggers_retry(self):
        client = FakeClient(
            write_response(STILL_WRONG_CODE),
            reply("代码改好啦，测试通过"),
            write_response(CORRECT_CODE),
            reply("修复完成"),
        )
        answer = self.run_agent(client)
        self.assertIn("Python 验证成功", answer)
        self.assertEqual(len(client.requests), 4)

    def test_api_error_rolls_back(self):
        client = FakeClient(
            write_response(STILL_WRONG_CODE),
            RuntimeError("模拟的网络错误"),
        )
        with self.assertRaisesRegex(RuntimeError, "模拟的网络错误"):
            self.run_agent(client)
        self.assertEqual(
            (self.root / "calc.py").read_text(encoding="utf-8"),
            WRONG_CODE,
        )

    def test_no_tests_does_not_pass(self):
        (self.root / "test_calc.py").write_text(
            "# 没有任何测试\n", encoding="utf-8"
        )
        result = agent.execute_tests()
        self.assertEqual(result["test_count"], 0)
        self.assertFalse(result["passed"])

    def test_python_enforces_readonly(self):
        with self.assertRaises(PermissionError):
            agent.run_tool(
                "write_file",
                {"path": "calc.py", "content": CORRECT_CODE},
                allow_write=False,
            )


    def test_batch_calls_keep_all_tool_call_pairs(self):
        # 一次模型返回四个写操作。第三次失败触顶，第四个必须回复取消，
        # 保证原始 Session 中每个 call_id 都有对应 tool 结果。
        batch = [
            ("write_file", {
                "path": "calc.py",
                "content": STILL_WRONG_CODE,
            })
            for _ in range(4)
        ]
        client = FakeClient(reply(tool_requests=batch))
        answer = self.run_agent(client)
        self.assertIn("自动修复失败", answer)
        history = self.sessions.load(self.sid)
        tool_messages = [m for m in history if m["role"] == "tool"]
        self.assertEqual(len(tool_messages), 4)
        self.assertIn("工具未执行", tool_messages[-1]["content"])
        self.assertEqual(
            (self.root / "calc.py").read_text(encoding="utf-8"),
            WRONG_CODE,
        )

    def test_model_refuses_retry_eventually_rolls_back(self):
        client = FakeClient(
            write_response(STILL_WRONG_CODE),
            reply("我不想继续了"),
            reply("我还是不想继续了"),
            reply("不再修复"),
        )
        answer = self.run_agent(client)
        self.assertIn("修复失败", answer)
        self.assertIn("已回滚", answer)
        self.assertEqual(
            (self.root / "calc.py").read_text(encoding="utf-8"),
            WRONG_CODE,
        )



if __name__ == "__main__":
    unittest.main(verbosity=2)
