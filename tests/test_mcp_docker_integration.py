"""Checks for the MCP -> Docker shared test entrypoint (no Docker required)."""
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

from integrations import test_runner


class DockerRoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.repo = Path(__file__).resolve().parents[1] / "demo_repo"

    def assert_delegates(self, result):
        with (
            patch.object(test_runner, "run_tests_in_docker", return_value=result) as docker,
            patch("subprocess.run", side_effect=AssertionError("host subprocess forbidden")),
            patch("subprocess.Popen", side_effect=AssertionError("host subprocess forbidden")),
        ):
            actual = test_runner.run_unittest(self.repo)
        self.assertIs(actual, result)
        docker.assert_called_once_with(self.repo)

    def test_pass_is_forwarded(self):
        self.assert_delegates(dict(status="PASS", passed=True, returncode=0,
                                   test_count=2, output="OK"))

    def test_fail_is_forwarded(self):
        self.assert_delegates(dict(status="FAIL", passed=False, returncode=1,
                                   test_count=2, output="FAILURES"))

    def test_docker_error_has_no_host_fallback(self):
        self.assert_delegates(dict(status="ERROR", passed=False, returncode=None,
                                   test_count=0, output="Docker unavailable"))

    def test_timeout_has_no_host_fallback(self):
        self.assert_delegates(dict(status="TIMEOUT", passed=False, returncode=None,
                                   test_count=0, output="Timed out"))

    def test_string_repo_path_is_normalized(self):
        with patch.object(test_runner, "run_tests_in_docker", return_value={"status": "ERROR"}) as docker:
            test_runner.run_unittest(str(self.repo))
        docker.assert_called_once_with(self.repo)

    def test_direct_script_style_import(self):
        # Works when the MCP Server imports test_runner as an unqualified module.
        path = Path(test_runner.__file__).resolve()
        spec = importlib.util.spec_from_file_location("standalone_test_runner_check", path)
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertTrue(callable(mod.run_unittest))


if __name__ == "__main__":
    unittest.main()
