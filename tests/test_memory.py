import io
import json
import unittest
from contextlib import redirect_stderr
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import UUID

from mini_claude.memory import (
    build_memory_prompt_section,
    list_memories,
    read_memory,
    save_memory,
)
from mini_claude.prompt import build_system_prompt


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.workspace = Path.cwd().resolve()
        temporary = TemporaryDirectory(prefix=".memory-check-", dir=self.workspace)
        self.root = Path(temporary.name).resolve()
        if not self.root.is_relative_to(self.workspace):
            raise AssertionError("Temporary directory escaped the workspace")
        self.addCleanup(temporary.cleanup)

    def test_missing_memory_produces_no_prompt_section_or_directory(self):
        directory = self.root / "missing"
        self.assertEqual(build_memory_prompt_section(directory), "")
        self.assertFalse(directory.exists())

    def test_prompt_section_contains_metadata_and_read_instructions_but_not_body(self):
        memory_id = save_memory(
            self.root, title="Python 学习偏好", content="UNIQUE_BODY_ONLY_MARKER",
        )
        section = build_memory_prompt_section(self.root)
        self.assertIn(str(self.root), section)
        self.assertIn("read_file", section)
        self.assertIn("Python 学习偏好", section)
        self.assertNotIn("UNIQUE_BODY_ONLY_MARKER", section)
        index = json.loads(section.split("\n\n", 1)[1])
        self.assertEqual(index[0]["id"], memory_id)
        self.assertEqual(set(index[0]), {"id", "title", "created_at"})

    def test_system_prompt_loads_current_project_index_and_refreshes_on_rebuild(self):
        project = self.root / "project-a"
        other_project = self.root / "project-b"
        project.mkdir()
        other_project.mkdir()
        directory = project / ".mini-claude" / "memory"
        other_id = save_memory(
            other_project / ".mini-claude" / "memory", title="OTHER_PROJECT_TITLE",
            content="OTHER_PROJECT_BODY",
        )
        with patch("mini_claude.prompt.get_git_context", return_value=""):
            empty = build_system_prompt(project)
            self.assertNotIn("长期记忆索引", empty)
            memory_id = save_memory(directory, title="THIS_PROJECT_TITLE", content="BODY_NOT_IN_PROMPT")
            populated = build_system_prompt(project)
            self.assertIn(memory_id, populated)
            self.assertIn("THIS_PROJECT_TITLE", populated)
            self.assertNotIn("BODY_NOT_IN_PROMPT", populated)
            self.assertNotIn(other_id, populated)
            self.assertNotIn("OTHER_PROJECT_TITLE", populated)
            second_id = save_memory(directory, title="NEW_MEMORY_TITLE", content="new body")
            refreshed = build_system_prompt(project)
            self.assertIn(second_id, refreshed)
            self.assertIn("NEW_MEMORY_TITLE", refreshed)

    def test_save_and_read_round_trip_with_unicode_and_uuid_normalization(self):
        directory = self.root / "memory"
        memory_id = save_memory(directory, title="  学习偏好  ", content="  中文逐步教学。  ")
        memory = read_memory(directory, str(UUID(memory_id)))
        self.assertEqual(memory["id"], memory_id)
        self.assertEqual(memory["version"], 1)
        self.assertEqual(memory["title"], "学习偏好")
        self.assertEqual(memory["content"], "中文逐步教学。")
        self.assertIsNotNone(datetime.fromisoformat(memory["created_at"]).utcoffset())
        path = directory / f"{memory_id}.json"
        self.assertIn("中文逐步教学", path.read_text(encoding="utf-8"))
        path.write_text(json.dumps(memory, ensure_ascii=False), encoding="utf-8-sig")
        self.assertEqual(read_memory(directory, memory_id), memory)

    def test_invalid_title_or_content_is_rejected_without_creating_directory(self):
        directory = self.root / "not-created"
        for field in ("title", "content"):
            for value in ("", " \n", None, 42):
                with self.subTest(field=field, value=value):
                    arguments = {"title": "title", "content": "body", field: value}
                    with self.assertRaises(ValueError):
                        save_memory(directory, **arguments)
                    self.assertFalse(directory.exists())

    def test_new_memory_never_overwrites_an_existing_file(self):
        fixed_id = "a" * 32
        with patch("mini_claude.memory.uuid4", return_value=SimpleNamespace(hex=fixed_id)):
            save_memory(self.root, title="first", content="keep this")
            path = self.root / f"{fixed_id}.json"
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                save_memory(self.root, title="second", content="must not overwrite")
        self.assertEqual(path.read_bytes(), original)

    def test_bad_ids_missing_files_and_invalid_records_are_rejected(self):
        for memory_id in ("../outside", "not-a-uuid", None, 42):
            with self.subTest(memory_id=memory_id), self.assertRaises(ValueError):
                read_memory(self.root, memory_id)
        with self.assertRaises(ValueError):
            read_memory(self.root, "0" * 32)

        memory_id = save_memory(self.root, title="title", content="body")
        path = self.root / f"{memory_id}.json"
        valid = read_memory(self.root, memory_id)
        cases = [[], None, {**valid, "version": 2}, {**valid, "id": "0" * 32}]
        for field in ("title", "content", "created_at"):
            cases.extend([{**valid, field: " "}, {**valid, field: 42}])
        for value in cases:
            with self.subTest(value=value):
                path.write_text(json.dumps(value), encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_memory(self.root, memory_id)
        path.write_text("{broken JSON", encoding="utf-8")
        with self.assertRaises(ValueError):
            read_memory(self.root, memory_id)
        path.write_bytes(b"\xff\xfe\xff")
        with self.assertRaises(ValueError):
            read_memory(self.root, memory_id)

    def test_resolved_path_outside_memory_directory_is_rejected_before_reading(self):
        outside = self.workspace.parent / "outside-memory.json"
        with patch.object(Path, "resolve", side_effect=[self.root, outside]), \
                patch.object(Path, "read_text") as read:
            with self.assertRaises(ValueError):
                read_memory(self.root, "a" * 32)
            read.assert_not_called()

    def test_list_missing_or_empty_directory_and_reject_file_as_directory(self):
        missing = self.root / "missing"
        self.assertEqual(list_memories(missing), [])
        self.assertFalse(missing.exists())
        self.assertEqual(list_memories(self.root), [])
        file = self.root / "plain-file"
        file.write_text("text", encoding="utf-8")
        with self.assertRaises(ValueError):
            list_memories(file)

    def test_list_returns_sorted_metadata_and_skips_invalid_files(self):
        bravo = save_memory(self.root, title="Bravo", content="private full content")
        alpha = save_memory(self.root, title="alpha", content="another full memory")
        bad_id = "f" * 32
        (self.root / f"{bad_id}.json").write_text("{broken", encoding="utf-8")
        (self.root / "not-an-id.json").write_text("{}", encoding="utf-8")
        (self.root / "directory.json").mkdir()
        errors = io.StringIO()
        with redirect_stderr(errors):
            items = list_memories(self.root)
        self.assertEqual([item["id"] for item in items], [alpha, bravo])
        self.assertEqual([item["title"] for item in items], ["alpha", "Bravo"])
        for item in items:
            self.assertEqual(set(item), {"id", "title", "created_at"})
        self.assertIn(bad_id, errors.getvalue())
        self.assertIn("not-an-id.json", errors.getvalue())
        self.assertNotIn("private full content", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
