"""Docker result classification regression (mocked CLI, no Docker required)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sandbox import docker_runner


class DockerResultClassificationTests(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        self.root = Path(t.name)
        (self.root / "calc.py").write_text("def add(a,b): return a+b\n", encoding="utf-8")
        (self.root / "test_calc.py").write_text("import unittest\n", encoding="utf-8")

    def run_mocked(self, code, log, timed_out=False):
        with (
            patch.object(docker_runner.shutil, "which", return_value="docker"),
            patch.object(docker_runner, "_execute_command",
                         return_value=(code, log, timed_out)),
        ):
            return docker_runner.run_tests_in_docker(self.root)

    def test_docker_api_failure_is_error_not_fail(self):
        r = self.run_mocked(1, "failed to connect to the docker API at npipe")
        self.assertEqual(r["status"], "ERROR")
        self.assertFalse(r["passed"])
        self.assertEqual(r["returncode"], 1)
        self.assertEqual(r["test_count"], 0)

    def test_real_assertion_failure_is_fail(self):
        r = self.run_mocked(1, "test_add ... FAIL\nRan 2 tests in 0.01s\nFAILED")
        self.assertEqual(r["status"], "FAIL")
        self.assertEqual(r["test_count"], 2)

    def test_pass_requires_discovered_tests(self):
        r = self.run_mocked(0, "test_add ... ok\nRan 2 tests in 0.01s\nOK")
        self.assertEqual(r["status"], "PASS")
        self.assertTrue(r["passed"])

    def test_zero_tests_cannot_pass(self):
        r = self.run_mocked(0, "Ran 0 tests in 0.001s\nOK")
        self.assertEqual(r["status"], "ERROR")
        self.assertFalse(r["passed"])

    def test_timeout_remains_timeout(self):
        r = self.run_mocked(None, "partial log", timed_out=True)
        self.assertEqual(r["status"], "TIMEOUT")
        self.assertFalse(r["passed"])

    def test_docker_cli_125_remains_error(self):
        r = self.run_mocked(125, "docker: daemon unavailable")
        self.assertEqual(r["status"], "ERROR")


if __name__ == "__main__":
    unittest.main(verbosity=2)
