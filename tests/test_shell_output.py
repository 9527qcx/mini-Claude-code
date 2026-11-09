import os
import shutil
import sys
import unittest
from pathlib import Path

from mini_claude.shell import run_shell


@unittest.skipUnless(
    os.name == "nt" and shutil.which("powershell.exe"),
    "Windows PowerShell is required",
)
class ShellOutputTests(unittest.TestCase):
    def test_location_object_is_formatted_before_exit(self):
        result = run_shell("Get-Location", timeout=5)

        self.assertFalse(result["is_error"], result)
        self.assertIn(str(Path.cwd()), result["content"])

    def test_plain_text_and_unicode_output_are_preserved(self):
        text = "你好，Shell"
        result = run_shell(f"Write-Output '{text}'", timeout=5)

        self.assertFalse(result["is_error"], result)
        self.assertIn(text, result["content"])

    def test_explicit_exit_code_is_preserved(self):
        result = run_shell("exit 7", timeout=5)

        self.assertTrue(result["is_error"], result)
        self.assertIn("退出码：7", result["content"])

    def test_native_child_exit_code_and_output_are_preserved(self):
        executable = "'" + sys.executable.replace("'", "''") + "'"
        source = "import sys; print(12345); sys.exit(9)"
        result = run_shell(f"& {executable} -c '{source}'", timeout=5)

        self.assertTrue(result["is_error"], result)
        self.assertIn("退出码：9", result["content"])
        self.assertIn("12345", result["content"])

    def test_powershell_error_is_not_reported_as_success(self):
        result = run_shell("throw 'shell-output-test-error'", timeout=5)

        self.assertTrue(result["is_error"], result)
        self.assertIn("shell-output-test-error", result["content"])


if __name__ == "__main__":
    unittest.main()
