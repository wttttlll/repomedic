"""RepoMedic Skill 注册表与渐进式加载器。

启动时仅发现本地 Skill 元数据。
完整 SKILL.md 指令由 Agent 通过 load_skill 工具按需读取。
Skill 不授予额外的 Python 工具权限。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


MAX_SKILL_BYTES = 32 * 1024
SKILL_NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")


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
    """只读取 SKILL.md 的前置元数据。"""

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
            attributes[key.strip()] = (value.strip().strip("\"'"))
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
        """扫描本地 Skills，只读取元数据。"""

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
        """按需读取 Skill 的完整指令。"""

        path = info.path

        if (
            path.is_symlink()
            or not path.resolve().is_relative_to(self.root)
            or path.stat().st_size > MAX_SKILL_BYTES
        ):
            raise ValueError("不安全的技能路径或技能文件过大")

        name, description = _read_header(path)

        if name != info.name:
            raise ValueError("加载前技能元数据已变更")

        text = path.read_text(encoding="utf-8")
        lines = text.splitlines()

        try:
            end = next(
                i
                for i, line in enumerate(lines[1:], 1)
                if line.strip() == "---"
            )
        except StopIteration as exc:
            raise ValueError("未闭合的技能前置元数据") from exc

        body = "\n".join(lines[end + 1:]).strip()

        if not body:
            raise ValueError("技能指令体为空")

        return LoadedSkill(
            name=name,
            description=description,
            instructions=body,
        )

    def format_skill_catalog(self) -> str:
        """生成供主 Agent 使用的 Skill 元数据目录。"""

        skills = self.discover()

        if not skills:
            return ""

        lines = [
            "# Available Skills",
            "",
            "以下是当前可用的 Skills。",
            "这里只提供名称和描述，不包含完整指令。",
            "",
            "当任务适合某个 Skill 时，",
            "请调用 load_skill 工具读取完整指令。",
            "如果没有合适的 Skill，可以直接完成任务。",
            "不要虚构不存在的 Skill。",
            "",
        ]

        for skill in skills:
            lines.append(f"- name: {skill.name}")
            lines.append(f"  description: {skill.description}")

        return "\n".join(lines)

    def load_by_name(self, name: str) -> LoadedSkill:
        """根据 Skill 名称加载完整指令。"""

        for skill in self.discover():
            if skill.name == name:
                return self.load(skill)

        raise ValueError(f"未知的 Skill: {name}")


def format_skill_context(skill: LoadedSkill) -> str:
    """格式化 Skill 指令，不扩展工具权限。"""

    return (
        "\n\n[RepoMedic 技能: " + skill.name + "]\n"
        "本技能仅描述任务工作流程。"
        "它不授予写入权限、文件访问权限"
        "或跳过验证的权限。"
        "Python 运行时强制执行所有安全规则。\n"
        + skill.instructions
        + "\n[/RepoMedic 技能]\n"
    )


if __name__ == "__main__":
    registry = SkillLoader(Path(__file__).resolve().parent)

    found = registry.discover()

    print(
        "发现的技能:",
        [skill.name for skill in found],
    )

    print("\n=== Skill Catalog ===")
    print(registry.format_skill_catalog())

    if found:
        chosen = registry.load_by_name(found[0].name)

        print("\n=== 按需加载测试 ===")
        print("加载的 Skill:", chosen.name)
        print(
            "指令预览:",
            chosen.instructions[:120].replace("\n", " "),
        )