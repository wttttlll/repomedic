"""测试结果的结构化验证：模型文本不能替代真实的测试证据。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable


def checked_test_result(value: object) -> dict:
    """仅当状态、退出码、用例数量等一致时接受 PASS。"""
    def error(message: str) -> dict:
        return {
            "status": "ERROR",
            "passed": False,
            "returncode": None,
            "test_count": 0,
            "output": message,
        }

    if not isinstance(value, dict):
        return error("测试执行器未返回结构化字典结果")

    status = value.get("status")
    code = value.get("returncode")
    count = value.get("test_count")
    passed = value.get("passed")
    output = value.get("output", "")

    if status not in {"PASS", "FAIL", "ERROR", "TIMEOUT"}:
        return error("未知测试状态：" + str(status))
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return error("测试数量字段无效：" + str(count))
    if not isinstance(output, str):
        return error("测试日志不是字符串")
    if code is not None and (not isinstance(code, int) or isinstance(code, bool)):
        return error("退出码字段无效")

    if status == "PASS" and not (passed is True and code == 0 and count > 0):
        return error("测试结果声称 PASS，但缺少真实通过证据：" + output[:2000])
    if status == "FAIL" and count == 0:
        return error("没有执行任何单元测试，无法认定为代码断言失败：" + output[:2000])
    if status != "PASS" and passed is True:
        return error("测试状态与 passed 字段矛盾")

    return {
        "status": status,
        "passed": status == "PASS",
        "returncode": code,
        "test_count": count,
        "output": output,
    }


def execute_tests(
    root: Path,
    *,
    runner: Callable[[Path], Any],
    checked_result: Callable[[object], dict] = checked_test_result,
) -> dict:
    """调用原有 Docker Runner；绝不在测试容器失败时退回宿主机。"""
    return checked_result(runner(root))
