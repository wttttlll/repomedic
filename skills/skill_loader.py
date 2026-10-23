"""RepoMedic  渐进式披露，本地技能注册表与延迟指令加载器。
仅扫描开发者控制的本地 SKILL.md 文件。技能用于指导大语言模型，但不会授予任何能力；
Python 工具权限仍然是权威来源。这是一个仅使用标准库的 YAML 前置元数据子集实现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

MAX_SKILL_BYTES = 32 * 1024
SKILL_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")

# 规则路由：只有 repo-debug 是自动激活的。其他技能始终可以通过 @技能名称 显式激活。
# 基于模型的后续路由将在未来版本中添加。
AUTO_TRIGGERS: dict[str, tuple[str, ...]] = {
    "repo-debug": (
        "bug", "debug", "traceback",
        "\u4fee\u590d",             # 修复
        "\u6392\u67e5",             # 排查
        "\u62a5\u9519",             # 报错
        "\u8c03\u8bd5",             # 调试
        "\u4ee3\u7801\u8bca\u65ad",  # 代码诊断
        "\u6d4b\u8bd5\u5931\u8d25", # 测试失败
    )
}


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path


@dataclass(frozen=True)
class LoadedSkill:
    name: str
    description: str
    instructions: str


def _read_header(path: Path) -> tuple[str, str]:
    """仅读取类 YAML 的前置元数据；无需第三方依赖。"""
    if path.stat().st_size > MAX_SKILL_BYTES:
        raise ValueError(f"技能文件过大: {path}")

    with path.open("r", encoding="utf-8") as stream:
        if stream.readline().strip() != "---":
            raise ValueError(f"SKILL.md 必须以 --- 开头: {path}")
        attributes: dict[str, str] = {}
        for _ in range(30):
            line = stream.readline()
            if not line:
                raise ValueError(f"未闭合的技能前置元数据: {path}")
            if line.strip() == "---":
                break
            if not line.strip():
                continue
            key, separator, value = line.partition(":")
            if not separator:
                raise ValueError(f"无效的技能头部行: {line.strip()}")
            attributes[key.strip()] = value.strip().strip("\"'")
        else:
            raise ValueError(f"技能前置元数据过长: {path}")

    name = attributes.get("name", "")
    description = attributes.get("description", "")
    if not SKILL_NAME.fullmatch(name):
        raise ValueError(f"无效的技能名称: {name!r}")
    if name != path.parent.name:
        raise ValueError("技能名称必须与所在目录名称一致")
    if not description:
        raise ValueError(f"技能缺少描述信息: {path}")
    return name, description


class SkillLoader:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def discover(self) -> list[SkillInfo]:
        """读取技能的元数据，但不将内容送入大模型"""
        if not self.root.is_dir():
            return []

        result: list[SkillInfo] = []
        for folder in sorted(self.root.iterdir()):
            if not folder.is_dir() or folder.is_symlink():
                continue
            path = folder / "SKILL.md"
            if not path.is_file() or path.is_symlink():
                continue
            if not path.resolve().is_relative_to(self.root):
                continue
            name, description = _read_header(path)
            result.append(SkillInfo(name, description, path))
        return result

    def load(self, info: SkillInfo) -> LoadedSkill:
        """在需要使用时加载该技能的内容"""
        path = info.path
        if (path.is_symlink() or not path.resolve().is_relative_to(self.root)
                or path.stat().st_size > MAX_SKILL_BYTES):
            raise ValueError("不安全的技能路径或技能文件过大")
        name, description = _read_header(path)
        if name != info.name:
            raise ValueError("加载前技能元数据已变更")

        text = path.read_text(encoding="utf-8")

        lines = text.splitlines()
        try:
            end = next(i for i, line in enumerate(lines[1:], 1)
                       if line.strip() == "---")
        except StopIteration as exc:
            raise ValueError("未闭合的技能前置元数据") from exc
        body = "\n".join(lines[end + 1:]).strip()
        if not body:
            raise ValueError("技能指令体为空")
        return LoadedSkill(name, description, body)

    def select(self, task: str) -> LoadedSkill | None:

        if not isinstance(task, str):
            raise TypeError("任务参数必须是字符串")
        discovered = self.discover()
        for info in discovered:
            if re.search(r"(?<![\w-])@" + re.escape(info.name) + r"(?![\w-])",
                         task, flags=re.IGNORECASE):
                return self.load(info)
        lowered = task.casefold()
        for info in discovered:
            if any(word in lowered for word in AUTO_TRIGGERS.get(info.name, ())):
                return self.load(info)
        return None


def format_skill_context(skill: LoadedSkill) -> str:
    """追加到现有系统指令之后；绝不扩展工具权限。"""
    return (
        "\n\n[RepoMedic 技能: " + skill.name + "]\n"
        "本技能仅描述任务工作流程。它不授予写入权限、文件访问权限或跳过验证的权限。"
        "Python 运行时强制执行所有安全规则。\n"
        + skill.instructions + "\n[/RepoMedic 技能]\n"
    )

if __name__ == "__main__":
    registry = SkillLoader(Path(__file__).resolve().parent)
    found = registry.discover()
    print("发现的技能:", [s.name for s in found])
    sample_task = "\u4fee\u4e00\u4e0bbug"
    chosen = registry.select(sample_task)
    print("示例任务的选中结果:", chosen.name if chosen else "无")
    if chosen:
        print("指令预览:", chosen.instructions[:120].replace("\n", " "))