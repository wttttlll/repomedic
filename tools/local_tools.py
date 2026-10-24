"""本地 Python 工具。工具副作用和路径约束在此统一管理。"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable
from integrations.ppt_generator import (
    inspect_template,
    generate_presentation,
)

def run_tool(
    name: str,
    args: dict,
    allow_write: bool = False,
    *,
    root: Path,
    base: Path,
    max_file_bytes: int,
    execute_tests: Callable[[], dict],
) -> str:
    """执行本地工具；保留原有 JSON 字符串返回接口。"""
    if not isinstance(args, dict):
        raise ValueError("工具参数必须是 JSON 对象")

    if name == "list_files":
        if args:
            raise ValueError("list_files 不接受参数")
        if not root.is_dir():
            raise ValueError("demo_repo 目录不存在")
        return json.dumps(
            sorted(
                p.name + ("/" if p.is_dir() else "")
                for p in root.iterdir()
                if not p.is_symlink()
            ),
            ensure_ascii=False,
        )

    if name == "read_file":
        if set(args) != {"path"} or not isinstance(args["path"], str):
            raise ValueError("read_file 需要字符串参数 path")
        relative = Path(args["path"])
        if relative.is_absolute():
            raise ValueError("不能使用绝对路径")
        target = (root / relative).resolve()
        if not target.is_relative_to(root.resolve()):
            raise ValueError("禁止读取 demo_repo 外部的文件")
        if not target.is_file():
            raise ValueError("文件不存在")
        if target.stat().st_size > max_file_bytes:
            raise ValueError("文件超过 32 KiB")
        try:
            return target.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("仅支持 UTF-8 文本文件") from exc

    if name == "run_tests":
        if args:
            raise ValueError("run_tests 不接受参数")
        return json.dumps(execute_tests(), ensure_ascii=False)

    if name == "write_file":
        if not allow_write:
            raise PermissionError("当前是只读模式，禁止修改文件")
        if set(args) != {"path", "content"}:
            raise ValueError("write_file 参数不完整")
        path = args["path"]
        content = args["content"]
        # 多一层本地兜底：不能仅依赖 Hook 阻止测试文件修改。
        if path != "calc.py":
            raise PermissionError("只允许修改 calc.py")
        if not isinstance(content, str):
            raise ValueError("content 必须是字符串")
        data = content.encode("utf-8")
        if len(data) > max_file_bytes:
            raise ValueError("文件内容超过限制")

        target = root / "calc.py"
        if target.is_symlink() or not target.is_file():
            raise ValueError("目标必须是存在的普通文件")

        backup_dir = base / "data" / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"calc_{uuid.uuid4().hex}.bak"
        shutil.copy2(target, backup)

        temp = root / f".calc_{uuid.uuid4().hex}.tmp"
        try:
            temp.write_bytes(data)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return (
            f"成功修改 {path}。"
            f"备份位置: {backup}"
            "。请立即运行 run_tests 验证修改。"
        )

    if name == "inspect_ppt_template":
        if not set(args).issubset({"template"}):
            raise ValueError("模板检查参数不正确")

        result = inspect_template(
            base,
            template=args.get("template")
        )
        return json.dumps(result, ensure_ascii=False)

    if name == "generate_ppt":
        if not allow_write:
            raise PermissionError("需要文件写入授权")

        allowed_keys = {
            "filename",
            "slides",
            "palette",
            "template",
            "template_mode",
            "preview",
        }
        if not set(args).issubset(allowed_keys):
            raise ValueError("PPT 参数中存在未知字段")

        if "filename" not in args or "slides" not in args:
            raise ValueError("缺少 filename 或 slides")

        result = generate_presentation(
            base,
            filename=args["filename"],
            slides=args["slides"],
            palette=args.get("palette", "ocean"),
            template=args.get("template"),
            template_mode=args.get("template_mode", "replace"),
            preview=args.get("preview", True),
        )
        return json.dumps(result, ensure_ascii=False)
    raise ValueError(f"未知工具: {name}")
