
import unittest

from hooks.hook_manager import HookManager, HookDenied


class HookTests(unittest.TestCase):

    def setUp(self):
        self.logs = []
        self.hooks = HookManager(logger=self.logs.append)

    def test_readonly_can_read(self):
        self.hooks.pre_tool_use(
            "read_file",
            {"path": "calc.py"},
            allow_write=False,
        )
        self.assertIn("ALLOW", self.logs[-1])

    def test_readonly_cannot_write(self):
        with self.assertRaises(HookDenied):
            self.hooks.pre_tool_use(
                "write_file",
                {"path": "calc.py"},
                allow_write=False,
            )

    def test_authorized_write(self):
        self.hooks.pre_tool_use(
            "write_file",
            {"path": "calc.py"},
            allow_write=True,
        )
        self.assertIn("ALLOW", self.logs[-1])

    def test_cannot_modify_tests(self):
        with self.assertRaises(HookDenied):
            self.hooks.pre_tool_use(
                "write_file",
                {"path": "test_calc.py"},
                allow_write=True,
            )

    def test_post_tool_logging(self):
        self.hooks.post_tool_use(
            "run_tests",
            success=True,
        )
        self.assertIn("OK", self.logs[-1])

    def test_verification_failure(self):
        self.hooks.on_verification_fail("FAIL")
        self.assertIn("FAIL", self.logs[-1])

    def test_guard_success(self):
        """验证正常执行后的 Hook。"""

        with self.hooks.guard(
                "read_file",
                {"path": "calc.py"},
                allow_write=False,
        ):
            pass

        self.assertIn(
            "[PostToolUse] read_file: OK",
            self.logs,
        )

    def test_guard_error(self):
        """验证工具抛出异常后的 Hook。"""

        with self.assertRaises(RuntimeError):
            with self.hooks.guard(
                    "run_tests",
                    {},
                    allow_write=False,
            ):
                raise RuntimeError("模拟工具异常")

        self.assertIn(
            "[PostToolUse] run_tests: ERROR",
            self.logs,
        )

    def test_guard_denied_before_execution(self):
        """验证未授权操作不会执行。"""

        executed = []

        with self.assertRaises(HookDenied):
            with self.hooks.guard(
                    "write_file",
                    {"path": "calc.py"},
                    allow_write=False,
            ):
                executed.append(True)

        self.assertEqual(executed, [])


if __name__ == "__main__":
    unittest.main()