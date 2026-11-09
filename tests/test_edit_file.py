import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from mini_claude.tools import execute_tool


class EditFileTests(unittest.TestCase):
    def setUp(self):
        workspace = Path.cwd().resolve()
        self.temporary = TemporaryDirectory(
            prefix=".edit-check-", dir=workspace
        )
        self.test_directory = Path(self.temporary.name).resolve()
        if not self.test_directory.is_relative_to(workspace):
            raise AssertionError("Temporary directory escaped the workspace")
        self.addCleanup(self.temporary.cleanup)

        self.path = self.test_directory / "sample.txt"
        self.read_state = {}

    def read_file(self):
        result = execute_tool(
            "read_file",
            {"file_path": str(self.path)},
            read_state=self.read_state,
        )
        self.assertFalse(result["is_error"], result)

    def edit_file(self):
        return execute_tool(
            "edit_file",
            {
                "file_path": str(self.path),
                "old_string": "hello",
                "new_string": "changed",
            },
            read_state=self.read_state,
        )

    def test_edit_without_reading_is_rejected(self):
        original = b"hello\r\n"
        self.path.write_bytes(original)

        result = self.edit_file()

        self.assertTrue(result["is_error"], result)
        self.assertIn("read_file", result["content"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_multiple_matches_are_rejected(self):
        original = b"hello\r\nhello\r\n"
        self.path.write_bytes(original)
        self.read_file()

        result = self.edit_file()

        self.assertTrue(result["is_error"], result)
        self.assertIn("匹配多处", result["content"])
        self.assertEqual(self.path.read_bytes(), original)

    def test_external_modification_is_preserved(self):
        self.path.write_bytes(b"hello\r\n")
        self.read_file()
        key = str(self.path.resolve())
        recorded_mtime = self.read_state[key]

        # Simulate an editor saving the file after the agent read it.
        # Keep one match so rejection specifically exercises the mtime guard.
        externally_modified = b"hello\r\nexternal change\r\n"
        self.path.write_bytes(externally_modified)
        current_stat = self.path.stat()
        os.utime(
            self.path,
            ns=(current_stat.st_atime_ns, recorded_mtime + 2_000_000_000),
        )
        self.assertNotEqual(self.path.stat().st_mtime_ns, recorded_mtime)

        result = self.edit_file()

        self.assertTrue(result["is_error"], result)
        self.assertIn("外部修改", result["content"])
        self.assertEqual(self.path.read_bytes(), externally_modified)
        self.assertNotIn(key, self.read_state)

    def test_read_then_unique_edit_succeeds(self):
        self.path.write_bytes(b"hello\r\n")
        self.read_file()

        result = self.edit_file()

        self.assertFalse(result["is_error"], result)
        self.assertEqual(self.path.read_bytes(), b"changed\r\n")
        self.assertNotIn(str(self.path.resolve()), self.read_state)


if __name__ == "__main__":
    unittest.main()
