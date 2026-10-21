"""Skill discovery, matching and lazy loading; no network or LLM needed."""
import tempfile
import unittest
from pathlib import Path

from skills.skill_loader import SkillLoader, format_skill_context


class SkillLoaderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.skills = Path(temp.name) / "skills"
        folder = self.skills / "repo-debug"
        folder.mkdir(parents=True)
        self.file = folder / "SKILL.md"
        self.file.write_text(
            "---\nname: repo-debug\n"
            "description: Analyze and fix Python code bugs.\n---\n"
            "# Debug workflow\nRead files, run tests, and verify.\n",
            encoding="utf-8",
        )
        self.loader = SkillLoader(self.skills)

    def test_discover_reads_metadata(self):
        infos = self.loader.discover()
        self.assertEqual(len(infos), 1)
        self.assertEqual(infos[0].name, "repo-debug")
        self.assertIn("Python code", infos[0].description)

    def test_keyword_activates_repo_debug(self):
        skill = self.loader.select("\u4fee\u4e00\u4e0bbug")
        self.assertIsNotNone(skill)
        self.assertIn("run tests", skill.instructions)

    def test_explicit_activation(self):
        skill = self.loader.select("@repo-debug inspect calc.py")
        self.assertEqual(skill.name, "repo-debug")

    def test_irrelevant_task_does_not_activate_skill(self):
        self.assertIsNone(self.loader.select("What time is it?"))

    def test_invalid_name_or_directory_is_rejected(self):
        self.file.write_text(
            "---\nname: other\ndescription: Demo\n---\nHello\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            self.loader.discover()

    def test_oversized_skill_is_rejected(self):
        self.file.write_text(
            "---\nname: repo-debug\ndescription: A debug skill\n---\n"
            + ("A" * 34000), encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            self.loader.select("@repo-debug")

    def test_context_is_guidance_not_tool_grant(self):
        result = format_skill_context(self.loader.select("@repo-debug"))
        self.assertIn("does NOT grant write", result)
        self.assertIn("# Debug workflow", result)
        self.assertNotIn("description: Analyze", result)

    def test_untrusted_symlink_folder_is_skipped(self):
        link = self.skills / "another-debug"
        try:
            link.symlink_to(self.skills / "repo-debug", target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is not permitted")
        self.assertEqual(len(self.loader.discover()), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
