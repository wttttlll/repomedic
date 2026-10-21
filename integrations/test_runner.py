"""RepoMedic fixed-test entrypoint: Docker only, never host fallback."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# When mcp_server.py runs as a direct script, Python may only include
# integrations/ (not the project root) in sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from sandbox.docker_runner import run_tests_in_docker


def run_unittest(repo_root: Path) -> dict[str, Any]:
    """Keep the old MCP API, but always delegate execution to Docker.

    Return status / passed / returncode / test_count / output.
    An unavailable Docker daemon returns ERROR, never host execution.
    """
    return run_tests_in_docker(Path(repo_root))


if __name__ == "__main__":
    result = run_unittest(_PROJECT_ROOT / "demo_repo")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result.get("passed") is True else 1)
