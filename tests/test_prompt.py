import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from mini_claude.prompt import (
    MAX_INCLUDE_DEPTH,
    build_system_prompt,
    get_git_context,
    load_claude_md,
    load_rules_dir,
)


class ProjectRulesTests(unittest.TestCase):
    def setUp(self):
        workspace = Path.cwd().resolve()
        self.temporary = TemporaryDirectory(
            prefix=".include-check-", dir=workspace
        )
        self.test_directory = Path(self.temporary.name).resolve()
        if not self.test_directory.is_relative_to(workspace):
            raise AssertionError("Temporary directory escaped the workspace")
        self.addCleanup(self.temporary.cleanup)
        self.root = self.test_directory / "project"
        self.root.mkdir()

    def write_rule(self, relative_path, content):
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def test_missing_main_file_keeps_base_prompt_usable(self):
        self.assertEqual(load_claude_md(self.root), "")
        self.assertIn(str(self.root), build_system_prompt(self.root))

    def test_nested_paths_are_relative_to_the_including_file(self):
        self.write_rule("CLAUDE.md", "root-rule\n@./docs/a.md")
        self.write_rule("docs/a.md", "a-rule\n@./b.md")
        self.write_rule("docs/b.md", "nested-rule")

        rules = load_claude_md(self.root)

        self.assertEqual(rules, "root-rule\na-rule\nnested-rule")

    def test_cycle_is_rejected_with_the_reference_chain(self):
        self.write_rule("CLAUDE.md", "@./a.md")
        self.write_rule("a.md", "@./CLAUDE.md")

        with self.assertRaisesRegex(ValueError, "循环引用") as error:
            load_claude_md(self.root)

        self.assertIn("CLAUDE.md -> a.md -> CLAUDE.md", str(error.exception))

    def test_missing_included_file_reports_its_path(self):
        self.write_rule("CLAUDE.md", "@./missing.md")

        with self.assertRaisesRegex(ValueError, "读取项目规则失败") as error:
            load_claude_md(self.root)

        self.assertIn("missing.md", str(error.exception))

    def test_reference_outside_project_is_rejected(self):
        outside = self.test_directory / "outside.md"
        outside.write_text("outside-rule", encoding="utf-8")
        self.write_rule("CLAUDE.md", "@./../outside.md")

        with self.assertRaisesRegex(ValueError, "超出项目范围"):
            load_claude_md(self.root)

    def test_non_markdown_reference_is_rejected(self):
        self.write_rule("CLAUDE.md", "@./settings.txt")
        self.write_rule("settings.txt", "not-markdown")

        with self.assertRaisesRegex(ValueError, "Markdown"):
            load_claude_md(self.root)

    def test_shared_reference_is_not_a_cycle(self):
        self.write_rule("CLAUDE.md", "@./a.md\n@./b.md")
        self.write_rule("a.md", "@./common.md")
        self.write_rule("b.md", "@./common.md")
        self.write_rule("common.md", "shared-rule")

        self.assertEqual(
            load_claude_md(self.root), "shared-rule\nshared-rule"
        )

    def test_excessive_reference_depth_is_rejected(self):
        for index in range(MAX_INCLUDE_DEPTH + 1):
            name = "CLAUDE.md" if index == 0 else f"level-{index}.md"
            content = (
                f"@./level-{index + 1}.md"
                if index < MAX_INCLUDE_DEPTH
                else "end-rule"
            )
            self.write_rule(name, content)

        with self.assertRaisesRegex(ValueError, "层级过深"):
            load_claude_md(self.root)

    def test_rules_directory_uses_filename_order_and_markdown_files(self):
        self.write_rule(".claude/rules/20-second.md", "second-rule")
        self.write_rule(".claude/rules/01-first.md", "first-rule")
        self.write_rule(".claude/rules/notes.txt", "ignored-text")
        self.write_rule(".claude/rules/nested.md/child.md", "ignored-child")

        rules = load_rules_dir(self.root)

        self.assertLess(rules.index("first-rule"), rules.index("second-rule"))
        self.assertNotIn("ignored-text", rules)
        self.assertNotIn("ignored-child", rules)

    def test_rules_directory_is_loaded_without_claude_md(self):
        self.write_rule(".claude/rules/01-style.md", "directory-only-rule")

        with patch("mini_claude.prompt.get_git_context", return_value=""):
            prompt = build_system_prompt(self.root)

        self.assertFalse((self.root / "CLAUDE.md").exists())
        self.assertIn("directory-only-rule", prompt)

    def test_rules_directory_expands_relative_references(self):
        self.write_rule(
            ".claude/rules/01-style.md", "@./../../docs/shared.md"
        )
        self.write_rule("docs/shared.md", "included-directory-rule")

        rules = load_rules_dir(self.root)

        self.assertIn("included-directory-rule", rules)
        self.assertNotIn("@./", rules)

    def test_rules_directory_rejects_circular_references(self):
        self.write_rule(".claude/rules/01-a.md", "@./02-b.md")
        self.write_rule(".claude/rules/02-b.md", "@./01-a.md")

        with self.assertRaisesRegex(ValueError, "循环引用"):
            load_rules_dir(self.root)

    def test_git_context_contains_branch_and_preserves_status_columns(self):
        responses = [
            SimpleNamespace(stdout="main\n"),
            SimpleNamespace(stdout=" M tracked.py\n?? fresh.py\n"),
        ]
        with patch(
            "mini_claude.prompt.subprocess.run", side_effect=responses
        ) as run:
            context = get_git_context(self.root)

        self.assertIn("main", context)
        self.assertIn(" M tracked.py", context)
        self.assertIn("?? fresh.py", context)
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["cwd"], self.root)
            self.assertTrue(call.kwargs["check"])
            self.assertEqual(call.kwargs["timeout"], 2)

    def test_detached_head_and_clean_status_are_reported(self):
        responses = [SimpleNamespace(stdout="\n"), SimpleNamespace(stdout="")]
        with patch("mini_claude.prompt.subprocess.run", side_effect=responses):
            context = get_git_context(self.root)

        self.assertIn("分离 HEAD", context)
        self.assertIn("工作区干净", context)

    def test_git_failures_keep_base_prompt_usable(self):
        failures = [
            FileNotFoundError("git is unavailable"),
            subprocess.TimeoutExpired(["git"], 2),
            subprocess.CalledProcessError(128, ["git"]),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch(
                    "mini_claude.prompt.subprocess.run", side_effect=failure
                ):
                    self.assertEqual(get_git_context(self.root), "")
                    prompt = build_system_prompt(self.root)
                self.assertIn(str(self.root), prompt)


if __name__ == "__main__":
    unittest.main()
