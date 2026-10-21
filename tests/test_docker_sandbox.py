"""Docker Sandbox V1：无需 Docker Engine 的确定性单元测试。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandbox.docker_runner import (
    build_docker_command,
    run_tests_in_docker,
)


class DockerSandboxV1Tests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = Path(temp.name) / "demo_repo"
        self.repo.mkdir()
        (self.repo / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        (self.repo / "test_calc.py").write_text(
            "import unittest\nfrom calc import add\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_sum(self): self.assertEqual(add(2, 3), 5)\n",
            encoding="utf-8",
        )

    def test_command_has_security_limits(self):
        args = build_docker_command(
            self.repo, docker_bin="docker",
            container_name="repomedic-test-123", image="python:3.12-slim",
        )
        cmd = " ".join(args)
        for required in (
            "--network=none", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--user=65534:65534",
            "--pids-limit=64", "--memory=256m", "--cpus=1.0",
            "--pull=never", "readonly", "--tmpfs", "test_calc.py",
        ):
            self.assertIn(required, cmd)
        self.assertNotIn("--privileged", args)
        self.assertNotIn("DEEPSEEK_API_KEY", cmd)
        self.assertIn("src=" + str(self.repo.resolve()), cmd)
        self.assertIn("--rm", args)

    def test_missing_repo_fails_closed(self):
        result = run_tests_in_docker(self.repo / "missing")
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(result["passed"])

    def test_missing_docker_fails_closed(self):
        with patch("sandbox.docker_runner.shutil.which", return_value=None):
            result = run_tests_in_docker(self.repo)
        self.assertEqual(result["status"], "ERROR")

    @patch("sandbox.docker_runner.shutil.which", return_value="docker")
    @patch("sandbox.docker_runner._execute_command")
    def test_pass(self, execute, which):
        execute.return_value = (
            0, "test_sum ... ok\n\nRan 1 test in 0.001s\n\nOK", False
        )
        outcome = run_tests_in_docker(self.repo)
        self.assertTrue(outcome["passed"])
        self.assertEqual(outcome["status"], "PASS")
        self.assertEqual(outcome["test_count"], 1)

    @patch("sandbox.docker_runner.shutil.which", return_value="docker")
    @patch("sandbox.docker_runner._execute_command")
    def test_failed_assertion(self, execute, which):
        execute.return_value = (
            1, "test_sum ... FAIL\nRan 1 test in 0.001s\nFAILED", False
        )
        outcome = run_tests_in_docker(self.repo)
        self.assertFalse(outcome["passed"])
        self.assertEqual(outcome["status"], "FAIL")
        self.assertEqual(outcome["test_count"], 1)

    @patch("sandbox.docker_runner.shutil.which", return_value="docker")
    @patch("sandbox.docker_runner._execute_command")
    def test_zero_tests_is_never_pass(self, execute, which):
        execute.return_value = (0, "Ran 0 tests in 0.001s\nOK", False)
        result = run_tests_in_docker(self.repo)
        self.assertFalse(result["passed"])
        self.assertEqual(result["test_count"], 0)

    @patch("sandbox.docker_runner.shutil.which", return_value="docker")
    @patch("sandbox.docker_runner._execute_command")
    def test_docker_failure_is_error(self, execute, which):
        execute.return_value = (
            125, "docker: No such image: python:3.12-slim", False
        )
        result = run_tests_in_docker(self.repo)
        self.assertEqual(result["status"], "ERROR")
        self.assertFalse(result["passed"])

    @patch("sandbox.docker_runner.shutil.which", return_value="docker")
    @patch("sandbox.docker_runner._execute_command")
    def test_timeout_is_error_not_pass(self, execute, which):
        execute.return_value = (None, "partial test output", True)
        result = run_tests_in_docker(self.repo)
        self.assertEqual(result["status"], "TIMEOUT")
        self.assertFalse(result["passed"])

    def test_refuse_symlink_test_file(self):
        (self.repo / "test_calc.py").unlink()
        try:
            (self.repo / "test_calc.py").symlink_to(self.repo / "calc.py")
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不允许创建符号链接")
        result = run_tests_in_docker(self.repo)
        self.assertEqual(result["status"], "ERROR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
