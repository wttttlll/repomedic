"""RepoMedic Docker Sandbox V1.

单独运行：
    python -m sandbox.docker_runner

只挂载 demo_repo；不会修改 Agent / MCP / Session。
需要 Docker Desktop（Linux Containers）和本地 python:3.12-slim 镜像。
注意：容器隔离是纵深防御，不是对恶意代码的绝对安全保证。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any


IMAGE = "python:3.12-slim"
CONTAINER_WORKDIR = "/workspace"
TIMEOUT_SECONDS = 30
HEAD_LIMIT = 48 * 1024
TAIL_LIMIT = 16 * 1024


def _result(
    status: str,
    *,
    returncode: int | None = None,
    test_count: int = 0,
    output: str = "",
) -> dict[str, Any]:
    passed = status == "PASS" and returncode == 0 and test_count > 0
    return {
        "status": status,
        "passed": passed,
        "returncode": returncode,
        "test_count": test_count,
        "output": output[:6000],
    }


def _validate_repo(repo_root: Path) -> Path:
    repo = Path(repo_root).expanduser()
    if repo.is_symlink():
        raise ValueError("拒绝挂载符号链接指向的仓库目录")
    repo = repo.resolve(strict=True)
    if not repo.is_dir():
        raise ValueError("测试仓库不是目录")

    for name in ("calc.py", "test_calc.py"):
        target = repo / name
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"找不到普通文件 {name}，或目标是符号链接")
    return repo

# 决定容器有什么权限
def build_docker_command(
    repo_root: Path,
    *,
    docker_bin: str,
    container_name: str,
    image: str = IMAGE,
) -> list[str]:
    """创建可审计的 Docker 参数数组：不用 shell，不允许模型提供命令。"""
    repo = _validate_repo(repo_root)
    return [
        docker_bin, "run",
        "--rm",
        "--name", container_name,
        "--pull=never",  # 仅使用预先下载的镜像
        "--network=none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--user=65534:65534",
        "--pids-limit=64",
        "--memory=256m",
        "--cpus=1.0",
        "--ulimit=nofile=128:128",
        "--mount", f"type=bind,src={repo},dst={CONTAINER_WORKDIR},readonly",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=16m,mode=1777",
        "--workdir", CONTAINER_WORKDIR,
        "--env", "HOME=/tmp",
        "--env", "TMPDIR=/tmp",
        "--env", "PYTHONDONTWRITEBYTECODE=1",
        "--env", "PYTHONIOENCODING=utf-8",
        image,
        "python", "-I", "-B", "-m", "unittest",
        "discover",
        "-s", CONTAINER_WORKDIR,
        "-p", "test_calc.py",
        "-v",
    ]


def _force_remove_container(docker_bin: str, container_name: str) -> None:
    """超时或主进程中断时额外尝试清除容器，防止孤儿任务。"""
    try:
        subprocess.run(
            [docker_bin, "rm", "-f", container_name],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=6,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass

# 负责实际执行并控制运行过程
def _execute_command(
    command: list[str],
    timeout_seconds: float,
    docker_bin: str,
    container_name: str,
) -> tuple[int | None, str, bool]:
    """有上限地收集输出；超过时间限制后终止 CLI 并清理容器。"""
    proc = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        shell=False,
    )

    head = bytearray()
    tail = bytearray()
    total = [0]

    def drain() -> None:
        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(4096)
            if not chunk:
                break
            total[0] += len(chunk)
            if len(head) < HEAD_LIMIT:
                head.extend(chunk[: HEAD_LIMIT - len(head)])
            tail.extend(chunk)
            if len(tail) > TAIL_LIMIT:
                del tail[: len(tail) - TAIL_LIMIT]

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        try:
            proc.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                _force_remove_container(docker_bin, container_name)
        except KeyboardInterrupt:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            finally:
                _force_remove_container(docker_bin, container_name)
            raise
    finally:
        reader.join(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()

    if total[0] <= HEAD_LIMIT:
        output = bytes(head)
    else:
        output = (
            bytes(head)
            + b"\n... [middle output omitted] ...\n"
            + bytes(tail)
        )
    text = output.decode("utf-8", errors="replace")

    return proc.returncode if not timed_out else None, text, timed_out

# 负责判断测试成功、失败还是运行异常
def run_tests_in_docker(
    repo_root: Path,
    *,
    image: str = IMAGE,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """返回兼容 RepoMedic Verification V2 的测试结果字典。"""
    try:
        repo = _validate_repo(repo_root)
    except (ValueError, OSError) as exc:
        return _result("ERROR", output=f"测试仓库检查失败：{exc}")

    docker_bin = shutil.which("docker")
    if not docker_bin:
        return _result("ERROR", output="找不到 docker 命令，请安装或启动 Docker Desktop")

    if not 1 <= timeout_seconds <= 120:
        return _result("ERROR", output="timeout_seconds 必须在 1 到 120 秒之间")

    name = "repomedic-test-" + uuid.uuid4().hex[:12]
    command = build_docker_command(
        repo, docker_bin=docker_bin, container_name=name, image=image,
    )

    try:
        code, output, timed_out = _execute_command(
            command, timeout_seconds, docker_bin, name,
        )
    except OSError as exc:
        return _result("ERROR", output=f"Docker CLI 执行失败：{exc}")

    if timed_out:
        return _result(
            "TIMEOUT",
            output=f"Docker 测试执行超时（{timeout_seconds:g} 秒）；"
                   "已尝试强制删除容器。\n" + output,
        )

    # Docker CLI: 125=daemon/client error; 126/127=container command error.
    if code in {125, 126, 127}:
        return _result(
            "ERROR", returncode=code,
            output=(
                "Docker 无法正常启动测试容器。请检查镜像、Docker Desktop "
                "和文件挂载权限。\n" + output
            ),
        )
    if code == 137:
        return _result(
            "ERROR", returncode=code,
            output="容器被强制终止（可能触发内存限制）。\n" + output,
        )

    match = re.search(r"\bRan\s+(\d+)\s+tests?\s+in\b", output)
    if match is None:
        # An exit code of 1 is not always a unit-test failure. For example,
        # Docker Desktop might be stopped and return code 1 before Python starts.
        return _result(
            "ERROR", returncode=code,
            output="未获得 unittest 测试汇总（容器或测试环境可能未启动）。\n" + output,
        )

    count = int(match.group(1))
    if count == 0:
        return _result(
            "ERROR", returncode=code,
            output="实际执行的测试数量为 0，无法验证代码是否正确。\n" + output,
        )

    return _result(
        "PASS" if code == 0 else "FAIL",
        returncode=code,
        test_count=count,
        output=output,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="在只读 Docker Sandbox 中运行 RepoMedic 单元测试"
    )
    parser.add_argument(
        "--repo", type=Path,
        default=Path(__file__).resolve().parent.parent / "demo_repo",
        help="指定 demo_repo 路径，默认是项目根目录下的 demo_repo",
    )
    options = parser.parse_args()
    result = run_tests_in_docker(options.repo)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"PASS", "FAIL"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
