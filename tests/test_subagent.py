
import copy
import json
import unittest

from types import SimpleNamespace as N
from subagents.test_analyst import TestAnalystSubAgent


def fcall(name, arguments, call_id="call_1"):
    return N(
        id=call_id,
        function=N(
            name=name,
            arguments=json.dumps(arguments),
        ),
    )


def reply(calls=None, text=None):
    return N(
        choices=[
            N(
                finish_reason=(
                    "tool_calls" if calls else "stop"
                ),
                message=N(
                    content=text,
                    tool_calls=calls or [],
                ),
            )
        ]
    )


class FakeClient:

    def __init__(self, *answers):
        self.answers = iter(answers)
        self.received = []
        self.chat = N(
            completions=N(create=self.create)
        )

    def create(self, **kwargs):
        self.received.append(copy.deepcopy(kwargs))
        return next(self.answers)


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": name,
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    }
    for name in (
        "list_files",
        "read_file",
        "run_tests",
        "write_file",
    )
]


class SubagentTests(unittest.TestCase):

    def test_independent_read_only(self):
        client = FakeClient(
            reply([
                fcall(
                    "read_file",
                    {"path": "calc.py"},
                )
            ]),
            reply(text="根因是将 + 写成了 -"),
        )

        invocations = []

        sub = TestAnalystSubAgent(
            client=client,
            tool_definitions=SCHEMAS,
            executor=lambda n, a: (
                invocations.append((n, a))
                or "def add(a,b): return a-b"
            ),
        )

        outcome = sub.run("分析测试失败原因")

        self.assertEqual(
            outcome["status"],
            "completed",
        )
        self.assertEqual(
            invocations,
            [("read_file", {"path": "calc.py"})],
        )

        names = [
            t["function"]["name"]
            for t in client.received[0]["tools"]
        ]
        self.assertNotIn("write_file", names)

        roles = [
            m["role"]
            for m in client.received[0]["messages"]
        ]
        self.assertEqual(roles, ["system", "user"])

        self.assertEqual(
            client.received[1]["messages"][-1]["tool_call_id"],
            "call_1",
        )

    def test_block_write(self):
        client = FakeClient(
            reply([
                fcall(
                    "write_file",
                    {
                        "path": "calc.py",
                        "content": "evil",
                    },
                )
            ]),
            reply(text="不能写入"),
        )

        invocations = []

        sub = TestAnalystSubAgent(
            client=client,
            tool_definitions=SCHEMAS,
            executor=lambda n, a: invocations.append(n),
        )

        outcome = sub.run("分析测试失败")

        self.assertEqual(
            outcome["status"],
            "completed",
        )
        self.assertEqual(invocations, [])

        tool_result = client.received[1]["messages"][-1]
        self.assertIn(
            "PermissionError",
            tool_result["content"],
        )

    def test_calls_are_paired(self):
        client = FakeClient(
            reply([
                fcall("list_files", {}, "a"),
                fcall("run_tests", {}, "b"),
            ]),
            reply(text="分析完成"),
        )

        sub = TestAnalystSubAgent(
            client=client,
            tool_definitions=SCHEMAS,
            executor=lambda n, a: f"{n} result",
        )

        outcome = sub.run("分析失败")

        self.assertEqual(
            outcome["used_tools"],
            ["list_files", "run_tests"],
        )

        tool_ids = [
            m["tool_call_id"]
            for m in client.received[1]["messages"]
            if m["role"] == "tool"
        ]
        self.assertEqual(tool_ids, ["a", "b"])


if __name__ == "__main__":
    unittest.main()