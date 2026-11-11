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
        agent.summary_messages = [{"role": "user", "content": "existing summary"}]
        agent.summary_covered_count = len(self.history)
        self.second.write_text("{broken", encoding="utf-8")
        with self.assertRaises(ValueError):
            agent.restore_session(self.second)
        self.assertEqual(agent.messages, self.history)
        self.assertEqual(agent.session_path, self.first)
        self.assertEqual(agent.read_state, {"sample.txt": 123})
        self.assertEqual(agent.summary_messages, [{"role": "user", "content": "existing summary"}])
        self.assertEqual(agent.summary_covered_count, len(self.history))

    def test_restored_agent_can_use_tools_and_save_to_original_session(self):
        self.seed_sessions()
        other_before = self.second.read_bytes()
        sample = self.root / "sample.txt"
        sample.write_text("file marker 42", encoding="utf-8")
        agent = Agent()
        agent.read_state["stale.txt"] = 123
        agent.summary_messages = [{"role": "user", "content": "summary of another session"}]
        agent.summary_covered_count = 100
        agent.restore_session(self.first)
        self.assertEqual(agent.read_state, {})
        self.assertEqual(agent.summary_messages, [])
        self.assertEqual(agent.summary_covered_count, 0)

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

    def test_interrupted_tool_batch_preserves_results_and_can_be_restored(self):
        tool_request = ChatCompletionMessage(
            role="assistant", content=None,
            tool_calls=[{
                "id": f"call_{index}", "type": "function",
                "function": {"name": name, "arguments": "{}"},
            } for index, name in enumerate([
                "read_file", "write_file", "read_file",
            ])],
        )
        success = {"is_error": False, "content": "first tool completed"}

        for mode in ("confirmation_interrupt", "pending_cancel", "tool_cancel"):
            with self.subTest(mode=mode):
                agent = Agent(session_path=self.first)
                error_type = (
                    KeyboardInterrupt
                    if mode == "confirmation_interrupt" else asyncio.CancelledError
                )

                def confirm(prompt):
                    if mode == "confirmation_interrupt":
                        raise KeyboardInterrupt()
                    if mode == "pending_cancel":
                        asyncio.current_task().cancel()
                    return "y"

                async def interrupted_turn():
                    # Catch inside the task so the test runner itself is not interrupted.
                    with self.assertRaises(error_type):
                        await agent.chat("perform three tools")
                    if mode == "pending_cancel":
                        self.assertEqual(asyncio.current_task().cancelling(), 1)
                        asyncio.current_task().uncancel()

                with patch("mini_claude.agent.ask_model", new_callable=AsyncMock) as model, \
                        patch("mini_claude.agent.execute_tool") as execute, \
                        patch("builtins.input", side_effect=confirm), \
                        patch("builtins.print"):
                    model.return_value = tool_request
                    execute.side_effect = [success, asyncio.CancelledError()]
                    asyncio.run(interrupted_turn())

                self.assertEqual(model.await_count, 1)
                self.assertEqual(execute.call_count, 2 if mode == "tool_cancel" else 1)
                self.assertEqual(agent.turn_usage.missing_requests, 1)
                self.assertEqual(
                    [message["role"] for message in agent.messages],
                    ["user", "assistant", "tool", "tool", "tool"],
                )
                results = agent.messages[2:]
                self.assertEqual(
                    [message["tool_call_id"] for message in results],
                    ["call_0", "call_1", "call_2"],
                )
                self.assertEqual(json.loads(results[0]["content"]), success)
                for message in results[1:]:
                    result = json.loads(message["content"])
                    self.assertTrue(result["is_error"])
                    self.assertIn("中断", result["content"])
                    self.assertIn("再次执行前请检查实际状态", result["content"])

                # Loading validates that every tool call has exactly one result.
                saved = load_session(self.first)
                self.assertEqual(saved, agent.messages)
                restored = Agent()
                restored.restore_session(self.first)
                self.assertEqual(restored.messages, saved)

                final = ChatCompletionMessage(role="assistant", content="Continue safely")
                with patch("mini_claude.agent.ask_model", new_callable=AsyncMock) as model, \
                        patch("mini_claude.agent.execute_tool") as execute:
                    model.return_value = final
                    self.assertEqual(asyncio.run(restored.chat("continue")), "Continue safely")
                    execute.assert_not_called()
                    sent_history = model.call_args.args[0]
                    self.assertEqual(sent_history[:len(saved)], saved)
                self.assertEqual(load_session(self.first), restored.messages)

    def test_completed_tool_batch_survives_a_later_model_failure(self):
        tool_request = ChatCompletionMessage(
            role="assistant", content=None,
            tool_calls=[{
                "id": "call_read", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        )
        success = {"is_error": False, "content": "already completed"}

        for error_type in (RuntimeError, asyncio.CancelledError):
            with self.subTest(error=error_type.__name__):
                agent = Agent(session_path=self.first)
                saved_checkpoint = []

                async def model(messages, *, on_usage=None, on_context=None):
                    if len(messages) == 1:
                        return tool_request
                    self.assertIsNot(messages, agent.messages)
                    self.assertEqual(load_session(self.first), agent.messages)
                    saved_checkpoint.extend(agent.messages)
                    # A later, incomplete response must not change the checkpoint.
                    messages.append({"role": "assistant", "content": "incomplete"})
                    raise error_type("simulated model failure")

                async def failed_turn():
                    with self.assertRaises(error_type):
                        await agent.chat("read a file")

                with patch("mini_claude.agent.ask_model", new=model), \
                        patch("mini_claude.agent.execute_tool", return_value=success) as execute, \
                        patch("builtins.print"):
                    asyncio.run(failed_turn())

                execute.assert_called_once()
                self.assertEqual(len(saved_checkpoint), 3)
                self.assertEqual(agent.messages, saved_checkpoint)
                self.assertEqual(json.loads(agent.messages[-1]["content"]), success)
                self.assertEqual(load_session(self.first), saved_checkpoint)
                self.assertEqual(agent.turn_usage.missing_requests, 2)

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
