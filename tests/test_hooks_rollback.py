
"""RepoMedic Hooks + Rollback 集成测试。

使用模拟 LLM 和模拟测试结果。
不调用 DeepSeek、MCP 或 Docker。
只修改临时目录，不触碰真实 demo_repo。
"""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import agent
from hooks.hook_manager import HookManager


BAD = "def add(a, b):\n    return a - b\n"
OTHER_BAD = "def add(a, b):\n    return 404\n"
GOOD = "def add(a, b):\n    return a + b\n"


class FakeSessions:
    def __init__(self):
        self.history = {
            "testsession": [
                {
                    "role": "system",
                    "content": agent.SYSTEM_PROMPT,
                }
            ]
        }

    def load(self, session_id):
        return copy.deepcopy(self.history[session_id])

    def save(self, session_id, messages):
        self.history[session_id] = copy.deepcopy(messages)


def tool_reply(call_id, content=OTHER_BAD):
    call = SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name="write_file",
            arguments=json.dumps({
                "path": "calc.py",
                "content": content,
            }),
        ),
    )

    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            finish_reason="tool_calls",
            message=SimpleNamespace(
                content=None,
                tool_calls=[call],
            ),
        )],
    )


def text_reply(text="任务结束"):
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            finish_reason="stop",
            message=SimpleNamespace(
                content=text,
                tool_calls=[],
            ),
        )],
    )


class FakeClient:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(
                create=self.create,
            ),
        )

    def create(self, **kwargs):
        item = next(self.answers)

        if isinstance(item, BaseException):
            raise item

        return item


def verification(status):
    return {
        "status": status,
        "passed": status == "PASS",
        "returncode": (
            0 if status == "PASS"
            else 1 if status == "FAIL"
            else None
        ),
        "test_count": (
            2 if status in {"PASS", "FAIL"} else 0
        ),
        "output": f"模拟测试结果: {status}",
    }


class HooksRollbackTests(unittest.TestCase):

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)

        self.base = Path(temp.name)
        self.root = self.base / "demo_repo"
        self.root.mkdir()

        self.calc = self.root / "calc.py"
        self.calc.write_text(BAD, encoding="utf-8")

        self.sessions = FakeSessions()
        self.logs = []

        # 隔离真实文件操作和模型上下文压缩
        replacements = [
            (agent, "ROOT", self.root),
            (agent, "BASE", self.base),
            (
                agent,
                "HOOKS",
                HookManager(logger=self.logs.append),
            ),
            (
                agent.COMPRESSOR,
                "build",
                lambda **kwargs: kwargs["messages"],
            ),
            (agent, "run_tool", self.fake_run_tool),
            (
                agent,
                "_checked_test_result",
                lambda result: result,
            ),
        ]

        for obj, attr, value in replacements:
            patcher = patch.object(obj, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        if hasattr(agent, "SKILL_LOADER"):
            patcher = patch.object(
                agent.SKILL_LOADER,
                "select",
                return_value=None,
            )
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_run_tool(self, name, args, allow_write=False):
        if name != "write_file":
            raise AssertionError(f"意外工具: {name}")

        if not allow_write:
            raise AssertionError("只读写入到达了执行器")

        if args.get("path") != "calc.py":
            raise ValueError("禁止修改其他文件")

        self.calc.write_text(
            args["content"],
            encoding="utf-8",
        )

        return "write_file 执行成功"

    def run_fake(self, client, allow_write=True):
        return agent.run_agent(
            "检查并修复 calc.py",
            client,
            self.sessions,
            "testsession",
            allow_write=allow_write,
        )

    def test_readonly_denies_write(self):
        """测试1：未授权时必须拦截写入。"""

        client = FakeClient(
            tool_reply("call_1", GOOD),
            text_reply(),
        )

        with patch.object(agent, "execute_tests") as tests:
            self.run_fake(client, allow_write=False)

        self.assertEqual(
            self.calc.read_text(encoding="utf-8"),
            BAD,
        )

        tests.assert_not_called()

        history = self.sessions.load("testsession")
        tool_messages = [
            m for m in history if m["role"] == "tool"
        ]

        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(
            tool_messages[0]["tool_call_id"],
            "call_1",
        )
        self.assertIn(
            "HookDenied",
            tool_messages[0]["content"],
        )

    def test_three_failures_rollback(self):
        """测试2：连续三次 FAIL 必须回滚。"""

        client = FakeClient(
            tool_reply("call_1"),
            tool_reply("call_2"),
            tool_reply("call_3"),
        )

        with patch.object(
            agent,
            "execute_tests",
            return_value=verification("FAIL"),
        ) as tests:
            answer = self.run_fake(client)

        self.assertIn("自动修复失败", answer)

        self.assertEqual(
            self.calc.read_text(encoding="utf-8"),
            BAD,
        )

        self.assertEqual(tests.call_count, 3)

        self.assertEqual(
            sum(
                "[OnVerificationFail] FAIL" in x
                for x in self.logs
            ),
            3,
        )

    def test_docker_error_timeout_rollback(self):
        """测试3：ERROR/TIMEOUT 都必须停止并回滚。"""

        for status in ("ERROR", "TIMEOUT"):
            with self.subTest(status=status):

                self.calc.write_text(
                    BAD,
                    encoding="utf-8",
                )
                self.sessions = FakeSessions()

                client = FakeClient(
                    tool_reply(f"call_{status}")
                )

                with patch.object(
                    agent,
                    "execute_tests",
                    return_value=verification(status),
                ) as tests:
                    answer = self.run_fake(client)

                self.assertIn("验证环境异常", answer)

                self.assertEqual(
                    self.calc.read_text(encoding="utf-8"),
                    BAD,
                )

                tests.assert_called_once()

    def test_api_exception_rollback(self):
        """测试4：模型调用异常后恢复原始文件。"""

        client = FakeClient(
            tool_reply("call_1"),
            RuntimeError("模拟网络错误"),
        )

        with patch.object(
            agent,
            "execute_tests",
            return_value=verification("FAIL"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "模拟网络错误",
            ):
                self.run_fake(client)

        self.assertEqual(
            self.calc.read_text(encoding="utf-8"),
            BAD,
        )


if __name__ == "__main__":
    unittest.main()