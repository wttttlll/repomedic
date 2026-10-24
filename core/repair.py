"""自动修复状态机：快照、验证、重试、停止与回滚。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class RepairState:
    max_attempts: int = 3
    original_bytes: bytes | None = None
    write_attempts: int = 0
    retry_prompts: int = 0
    wrote_file: bool = False
    verified_passed: bool = False
    last_test: dict | None = None

    def before_write(self, root: Path) -> None:
        """在第一笔写入前保存快照；不允许已 PASS 或超次数后继续写入。"""
        if self.verified_passed:
            raise ValueError("代码已通过测试，禁止继续写入")
        if self.write_attempts >= self.max_attempts:
            raise ValueError("已达到最大修复次数")
        if self.original_bytes is None:
            target = root / "calc.py"
            if not target.is_file() or target.is_symlink():
                raise ValueError("calc.py 必须是普通文件")
            self.original_bytes = target.read_bytes()

    def after_write(
        self,
        *,
        execute_tests: Callable[[], dict],
        checked_result: Callable[[object], dict],
        on_verification_fail: Callable[[str], None],
    ) -> dict:
        """写入后立即用真实 Runner 验证，返回已校验的测试结果。"""
        self.wrote_file = True
        self.write_attempts += 1
        self.verified_passed = False

        try:
            raw_result = execute_tests()
        except Exception as exc:
            raw_result = {
                "status": "ERROR",
                "passed": False,
                "returncode": None,
                "test_count": 0,
                "output": f"运行测试失败：{type(exc).__name__}: {exc}",
            }

        self.last_test = checked_result(raw_result)
        self.verified_passed = self.last_test["passed"]
        status = self.last_test["status"]
        print(f"[Verification] 第 {self.write_attempts}/{self.max_attempts} 次：{status}")
        if status in {"FAIL", "ERROR", "TIMEOUT"}:
            on_verification_fail(status)
        return self.last_test

    def after_run_tests(
        self,
        value: object,
        *,
        checked_result: Callable[[object], dict],
        on_verification_fail: Callable[[str], None],
    ) -> dict:
        """将主 Agent 主动执行的 run_tests 纳入最近一次验证状态。"""
        result = checked_result(value)
        status = result["status"]
        if status in {"FAIL", "ERROR", "TIMEOUT"}:
            on_verification_fail(status)
        if self.wrote_file:
            self.last_test = result
            self.verified_passed = result["passed"]
        return result

    @property
    def infrastructure_issue(self) -> str | None:
        if self.wrote_file and self.last_test is not None:
            status = self.last_test.get("status")
            if status in {"ERROR", "TIMEOUT"}:
                return status
        return None

    @property
    def attempts_exhausted(self) -> bool:
        return (not self.verified_passed and self.write_attempts >= self.max_attempts)

    def should_retry_prompt(self, step: int, max_steps: int) -> bool:
        return (
            self.wrote_file
            and self.write_attempts < self.max_attempts
            and self.retry_prompts < 2
            and step < max_steps
        )

    def rollback(self, reason: str, restore_original: Callable[[bytes], None]) -> str:
        if self.original_bytes is None:
            return "本轮没有需要恢复的文件"
        restore_original(self.original_bytes)
        self.original_bytes = None
        print(f"[Rollback] 已恢复 calc.py：{reason}")
        return f"已回滚任务至开始前的版本。原因：{reason}"


def restore_file(root: Path, content: bytes) -> None:
    """在 repo 内原子恢复原始 calc.py，不跟随符号链接。"""
    import os
    import uuid

    target = root / "calc.py"
    if not target.is_file() or target.is_symlink():
        raise RuntimeError("目标文件缺失或变为符号链接，不能安全回滚")

    temporary = root / f".rollback_{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_bytes(content)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
