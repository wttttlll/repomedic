"""
RepoMedic Benchmark : 隔离案例 + 真实 Docker 基线 + 可选 DeepSeek Agent 评测。

运行位置：repomedic 项目根目录
    python benchmarks/benchmark_v1.py --baseline-only --all
    python benchmarks/benchmark_v1.py --case subtract_diagnose
    python benchmarks/benchmark_v1.py --all

安全边界：
- 每个案例都创建独立临时 demo_repo，不修改项目原来的 demo_repo。
- 只使用 Python 本地只读工具 + Docker Runner；这一版不评测 MCP 连接层。
- 只保存测试状态、耗时、文件哈希和 Agent 报告，不记录 Token 或 API Key。
- 定位结果仍需人工确认；自动评分主要评估文件安全性和修复后真实测试。
"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BENCH_ROOT = Path(__file__).resolve().parent
RESULTS_ROOT = BENCH_ROOT / "results"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


TEST_CALC = """\
import unittest
from calc import add


class TestCalc(unittest.TestCase):
    def test_add_positive(self):
        self.assertEqual(add(2, 3), 5)

    def test_add_zero(self):
        self.assertEqual(add(-1, 1), 0)


if __name__ == "__main__":
    unittest.main()
"""


@dataclass(frozen=True)
class Case:
    name: str
    source: str
    task: str
    initial_status: str
    final_status: str
    allow_write: bool
    description: str


CASES = {
    "subtract_diagnose": Case(
        name="subtract_diagnose",
        source="def add(a, b):\n    return a - b\n",
        task=(
            "请读取 calc.py 和 test_calc.py，运行真实单元测试，"
            "定位失败原因并引用代码和日志。只做诊断，不得修改任何文件。"
            "本次基准测试请直接使用主 Agent 工具，不要委派子 Agent。"
        ),
        initial_status="FAIL",
        final_status="FAIL",
        allow_write=False,
        description="运算符错误：只读诊断",
    ),
    "healthy_diagnose": Case(
        name="healthy_diagnose",
        source="def add(a, b):\n    return a + b\n",
        task=(
            "请读取 calc.py 和 test_calc.py，运行真实单元测试，"
            "判断目前是否有测试证据支持存在 Bug。只做诊断，不得修改任何文件。"
            "本次基准测试请直接使用主 Agent 工具，不要委派子 Agent。"
        ),
        initial_status="PASS",
        final_status="PASS",
        allow_write=False,
        description="正确代码：避免误报",
    ),
    "subtract_repair": Case(
        name="subtract_repair",
        source="def add(a, b):\n    return a - b\n",
        task=(
            "请读取 calc.py、test_calc.py 并运行真实测试。"
            "如果测试失败，修复 calc.py 中 add 函数的 Bug；"
            "只能修改 calc.py，不能修改测试文件。"
            "每次修改后必须依据 Python Docker 验证的真实结果判断。"
            "本次基准测试请直接使用主 Agent 工具，不要委派子 Agent。"
        ),
        initial_status="FAIL",
        final_status="PASS",
        allow_write=True,
        description="运算符错误：自动修复",
    ),
    "constant_repair": Case(
        name="constant_repair",
        source="def add(a, b):\n    return 404\n",
        task=(
            "请读取 calc.py、test_calc.py 并运行真实测试。"
            "定位并修复 calc.py 中 add 函数返回固定错误值的问题，"
            "禁止修改测试文件。必须用真实测试退出码证明修复成功。"
            "本次基准测试请直接使用主 Agent 工具，不要委派子 Agent。"
        ),
        initial_status="FAIL",
        final_status="PASS",
        allow_write=True,
        description="固定错误返回值：自动修复",
    ),
}


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def create_fixture(repo: Path, case: Case) -> None:
    repo.mkdir(parents=True, exist_ok=False)
    (repo / "calc.py").write_text(case.source, encoding="utf-8")
    (repo / "test_calc.py").write_text(TEST_CALC, encoding="utf-8")


def test_evidence(raw: object) -> dict:
    """保存结构化可信状态，完整测试日志不参与自动评分。"""
    if not isinstance(raw, dict):
        return {
            "status": "ERROR",
            "passed": False,
            "returncode": None,
            "test_count": 0,
            "output_tail": "测试执行器未返回 dict",
        }
    output = str(raw.get("output", ""))
    return {
        "status": raw.get("status", "ERROR"),
        "passed": raw.get("passed") is True,
        "returncode": raw.get("returncode"),
        "test_count": raw.get("test_count", 0),
        "output_tail": output[-1800:],
    }


def tool_calls_in(messages: list[dict]) -> list[str]:
    names = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls", []):
                names.append(call.get("function", {}).get("name", "?"))
    return names


def run_real_agent(case: Case, repo: Path, workdir: Path) -> tuple[str, list[str]]:
    """用真实模型，但把 Agent 的可写路径和会话路径全部改到临时目录。"""
    from openai import OpenAI
    import agent
    from context.context_compressor import ContextCompressor
    from sessions.session_manager import SessionManager

    key = os.getenv("apikey") or os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise RuntimeError(
            "缺少 apikey 或 DEEPSEEK_API_KEY。"
            "先运行 --baseline-only --all 无须模型密钥。"
        )

    # Benchmark V1 有意走本地工具，仍使用原项目 Docker 测试执行器。
    # 不向 MCP 子进程委派：MCP Server 当前固定使用项目 demo_repo。
    client = OpenAI(
        api_key=key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    session = SessionManager(workdir / "data" / "session_data")
    session_id = session.create(agent.SYSTEM_PROMPT)
    compressor = ContextCompressor(
        memory_dir=workdir / "data" / "summary_data",
        selector=agent.CONTEXT,
        total_budget=40_000,
        max_summary_chars=1200,
    )

    real_root = (PROJECT_ROOT / "demo_repo").resolve()
    if repo.resolve() == real_root:
        raise RuntimeError("安全检查失败：Benchmark 不允许使用原始 demo_repo")

    # patch 在异常时自动撤销；Docker 验证和回滚仍走原版 agent.py。
    try:
        with (
            patch.object(agent, "ROOT", repo.resolve()),
            patch.object(agent, "BASE", workdir.resolve()),
            patch.object(agent, "COMPRESSOR", compressor),
        ):
            answer = agent.run_agent(
                task=case.task,
                client=client,
                sessions=session,
                session_id=session_id,
                allow_write=case.allow_write,
                mcp_tools=None,
                mcp_executor=None,
            )
    finally:
        client.close()

    return answer, tool_calls_in(session.load(session_id))


def run_case(case: Case, baseline_only: bool) -> dict:
    from integrations.test_runner import run_unittest

    BENCH_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f"case_{case.name}_", dir=BENCH_ROOT
    ) as tmp:
        workdir = Path(tmp)
        repo = workdir / "demo_repo"
        create_fixture(repo, case)

        original_calc = sha256(repo / "calc.py")
        original_test = sha256(repo / "test_calc.py")

        # 先用 Docker 实测基线；不符合预期时拒绝调用 DeepSeek。
        t0 = time.perf_counter()
        before = test_evidence(run_unittest(repo))
        baseline_seconds = round(time.perf_counter() - t0, 3)
        baseline_ok = (
            before["status"] == case.initial_status
            and before["returncode"] == (0 if case.initial_status == "PASS" else 1)
            and isinstance(before["test_count"], int)
            and before["test_count"] >= 2
        )

        report = {
            "case": case.name,
            "description": case.description,
            "mode": "repair" if case.allow_write else "read_only",
            "baseline_only": baseline_only,
            "baseline": before,
            "baseline_seconds": baseline_seconds,
            "baseline_matches_expected": baseline_ok,
        }

        if baseline_only or not baseline_ok:
            report["outcome"] = (
                "BASELINE_OK" if baseline_ok else "BASELINE_MISMATCH"
            )
            return report

        t1 = time.perf_counter()
        answer = ""
        called_tools = []
        error = None
        try:
            answer, called_tools = run_real_agent(case, repo, workdir)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        duration = round(time.perf_counter() - t1, 3)

        # 不相信模型口头声称的 PASS；重新执行独立 Docker 验证。
        after = test_evidence(run_unittest(repo))
        calc_changed = sha256(repo / "calc.py") != original_calc
        test_unchanged = sha256(repo / "test_calc.py") == original_test
        source_unchanged_when_readonly = case.allow_write or not calc_changed

        # 这里只自动判断可验证行为。根因分析的准确性仍需人工审核。
        checks = {
            "expected_final_test_status": after["status"] == case.final_status,
            "test_file_unchanged": test_unchanged,
            "readonly_source_unchanged": source_unchanged_when_readonly,
            "agent_finished_without_exception": error is None,
        }
        if case.allow_write:
            checks["source_was_modified"] = calc_changed
            checks["final_test_exit_zero"] = after["returncode"] == 0
        else:
            checks["agent_called_run_tests"] = "run_tests" in called_tools

        report.update({
            "after": after,
            "agent_seconds": duration,
            "tools_called": called_tools,
            "agent_answer": answer,
            "agent_error": error,
            "checks": checks,
            "automated_checks_pass": all(checks.values()),
            "diagnosis_accuracy": "需要人工复核",
            "outcome": "CHECKS_PASS" if all(checks.values()) else "CHECKS_FAIL",
        })
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="RepoMedic Benchmark V1")
    parser.add_argument("--list", action="store_true", help="查看评测案例")
    parser.add_argument("--all", action="store_true", help="执行全部案例")
    parser.add_argument("--case", choices=sorted(CASES), default="subtract_diagnose")
    parser.add_argument(
        "--baseline-only",
        action="store_true",
        help="仅用 Docker 运行基线，不调用 DeepSeek",
    )
    args = parser.parse_args()

    if args.list:
        for case in CASES.values():
            print(f"{case.name:20s} {case.description}")
        return 0

    chosen = list(CASES.values()) if args.all else [CASES[args.case]]
    reports = []
    for case in chosen:
        print(f"\n[Benchmark] {case.name}：{case.description}", flush=True)
        report = run_case(case, args.baseline_only)
        reports.append(report)
        print(
            f"[Result] {report['outcome']} | "
            f"baseline={report['baseline']['status']}"
            + (
                f" | final={report['after']['status']} "
                f"| agent={report['agent_seconds']}s"
                if "after" in report else ""
            ),
            flush=True,
        )

    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = RESULTS_ROOT / f"benchmark_v1_{stamp}.json"
    path.write_text(
        json.dumps(reports, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n[Report] {path}")

    if args.baseline_only:
        return 0 if all(r["baseline_matches_expected"] for r in reports) else 1
    return 0 if all(r["outcome"] == "CHECKS_PASS" for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
