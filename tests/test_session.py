import asyncio
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import ANY, AsyncMock, patch

from openai.types.chat import ChatCompletionMessage

import mini_claude.__main__ as cli
from mini_claude.agent import Agent
from mini_claude.session import find_latest_session, load_session, save_session


class SessionTests(unittest.TestCase):
    def setUp(self):
        workspace = Path.cwd().resolve()
        self.temporary = TemporaryDirectory(prefix=".session-check-", dir=workspace)
        self.root = Path(self.temporary.name).resolve()
        if not self.root.is_relative_to(workspace):
            raise AssertionError("Temporary directory escaped the workspace")
        self.addCleanup(self.temporary.cleanup)
        self.sessions = self.root / ".mini-claude" / "sessions"
        self.history = [
            {"role": "user", "content": "Remember 42"},
            {"role": "assistant", "content": "42 remembered"},
        ]
        self.first = self.sessions / ("1" * 32 + ".json")
        self.second = self.sessions / ("2" * 32 + ".json")

    def seed_sessions(self):
        save_session(self.first, self.history)
        save_session(self.second, [])
        os.utime(self.first, ns=(2_000_000_000, 2_000_000_000))
        os.utime(self.second, ns=(1_000_000_000, 1_000_000_000))

    def test_latest_uses_save_time_and_ignores_temporary_files_and_directories(self):
        self.seed_sessions()
        (self.sessions / "unfinished.json.tmp").write_text("partial", encoding="utf-8")
        (self.sessions / "directory.json").mkdir()
        self.assertEqual(find_latest_session(self.sessions), self.first)

    def test_missing_or_empty_directory_has_a_clear_error(self):
        with self.assertRaisesRegex(ValueError, "没有可恢复"):
            find_latest_session(self.sessions)
        self.sessions.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "没有可恢复"):
            find_latest_session(self.sessions)

    def test_loader_rejects_incomplete_or_unmatched_tool_history(self):
        call = {
            "role": "assistant",
            "tool_calls": [{"id": "call_1"}],
        }
        result = {"role": "tool", "tool_call_id": "call_1", "content": "ok"}
        cases = [
            [call],
            [result],
            [call, self.history[0]],
            [call, result, result],
            [{"role": "assistant", "tool_calls": [{"id": "a"}, {"id": "a"}]}],
        ]
        for messages in cases:
            with self.subTest(messages=messages):
                save_session(self.first, messages)
                with self.assertRaises(ValueError):
                    load_session(self.first)

    def test_broken_json_is_reported(self):
        self.sessions.mkdir(parents=True)
        self.first.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "不是合法 JSON"):
            load_session(self.first)

    def test_failed_restore_preserves_existing_agent_state(self):
        save_session(self.first, self.history)
        agent = Agent()
        agent.restore_session(self.first)
        agent.read_state["sample.txt"] = 123
        self.second.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            agent.restore_session(self.second)
        self.assertEqual(agent.messages, self.history)
        self.assertEqual(agent.session_path, self.first)
        self.assertEqual(agent.read_state, {"sample.txt": 123})

    def test_restored_agent_can_use_tools_and_save_to_original_session(self):
        self.seed_sessions()
        other_before = self.second.read_bytes()
        sample = self.root / "sample.txt"
        sample.write_text("file marker 42", encoding="utf-8")
        agent = Agent()
        agent.read_state["stale.txt"] = 123
        agent.restore_session(self.first)
        self.assertEqual(agent.read_state, {})

        tool_request = ChatCompletionMessage(
            role="assistant",
            content=None,
            tool_calls=[{
                "id": "call_read",
                "type": "function",
                "function": {
                    "name": "read_file",
                    "arguments": json.dumps({"file_path": str(sample)}),
                },
            }],
        )
        answer = ChatCompletionMessage(role="assistant", content="Read 42")
        with patch("mini_claude.agent.ask_model", new_callable=AsyncMock) as model:
            model.side_effect = [tool_request, answer]
            with patch("builtins.print"):
                self.assertEqual(asyncio.run(agent.chat("Read the file")), "Read 42")
            self.assertEqual(model.await_count, 2)

        tool_result = json.loads(agent.messages[-2]["content"])
        self.assertFalse(tool_result["is_error"])
        self.assertIn("file marker 42", tool_result["content"])
        self.assertEqual(agent.messages[:2], self.history)
        self.assertEqual(load_session(self.first), agent.messages)
        self.assertEqual(self.second.read_bytes(), other_before)
        self.assertEqual(find_latest_session(self.sessions), self.first)

    def test_cli_new_session_does_not_restore(self):
        with patch.object(Path, "cwd", return_value=self.root), \
                patch.object(cli, "Agent") as factory, \
                patch("sys.argv", ["mini_claude", "hello"]), \
                patch("builtins.print"):
            factory.return_value.chat = AsyncMock(return_value="ok")
            cli.main()
            factory.return_value.restore_session.assert_not_called()
            factory.return_value.chat.assert_awaited_once_with("hello", on_text=ANY)
            path = factory.call_args.kwargs["session_path"]
            self.assertEqual(path.parent, self.sessions)
            self.assertEqual(len(path.stem), 32)

    def test_cli_specific_id_normalizes_both_uuid_formats(self):
        for value in ("1" * 32, "11111111-1111-1111-1111-111111111111"):
            with self.subTest(value=value), \
                    patch.object(Path, "cwd", return_value=self.root), \
                    patch.object(cli, "Agent") as factory, \
                    patch("sys.argv", ["mini_claude", "--resume", value, "hello"]), \
                    patch("builtins.print"):
                factory.return_value.chat = AsyncMock(return_value="ok")
                cli.main()
                factory.return_value.restore_session.assert_called_once_with(self.first)
                factory.return_value.chat.assert_awaited_once_with("hello", on_text=ANY)

    def test_cli_latest_supports_interactive_and_single_prompt(self):
        self.seed_sessions()
        cases = [
            (["--resume"], None),
            (["--resume=latest", "hello"], "hello"),
        ]
        for arguments, prompt in cases:
            with self.subTest(arguments=arguments), \
                    patch.object(Path, "cwd", return_value=self.root), \
                    patch.object(cli, "Agent") as factory, \
                    patch.object(cli, "run_repl") as repl, \
                    patch("sys.argv", ["mini_claude", *arguments]), \
                    patch("builtins.print"):
                factory.return_value.chat = AsyncMock(return_value="ok")
                cli.main()
                factory.return_value.restore_session.assert_called_once_with(self.first)
                if prompt is None:
                    repl.assert_called_once_with(factory.return_value)
                    factory.return_value.chat.assert_not_awaited()
                else:
                    factory.return_value.chat.assert_awaited_once_with(prompt, on_text=ANY)
                    repl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
