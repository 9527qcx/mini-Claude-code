import asyncio
import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai.types.chat import ChatCompletionMessage

from mini_claude.agent import Agent
from mini_claude.context import build_summary_messages, measure_context_chars
from mini_claude.session import load_session, save_session, validate_tool_pairs
from mini_claude.tools import TOOL_DEFINITIONS


def reply(text):
    return ChatCompletionMessage(role="assistant", content=text)


class CompactionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        workspace = Path.cwd().resolve()
        temporary = TemporaryDirectory(prefix=".compaction-check-", dir=workspace)
        self.root = Path(temporary.name).resolve()
        if not self.root.is_relative_to(workspace):
            raise AssertionError("Temporary directory escaped the workspace")
        self.addCleanup(temporary.cleanup)
        self.path = self.root / "session.json"
        self.enterContext(patch("mini_claude.agent.build_system_prompt", return_value="project rules"))
        self.enterContext(patch("mini_claude.agent.CONTEXT_TRIM_TRIGGER_CHARS", 6000))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.previous = [
            {"role": "user", "content": "Keep the task constraints"},
            {"role": "assistant", "content": "Original detailed findings " * 2000},
        ]
        self.agent = Agent(session_path=self.path)
        self.agent.messages = copy.deepcopy(self.previous)
        self.history = self.previous + [{"role": "user", "content": "continue"}]
        self.summary_usage = SimpleNamespace(
            prompt_tokens=100, completion_tokens=20, total_tokens=120,
        )
        self.chat_usage = SimpleNamespace(
            prompt_tokens=200, completion_tokens=30, total_tokens=230,
        )

    def context_size(self, messages):
        return measure_context_chars(
            [{"role": "system", "content": "project rules"}, *messages],
            TOOL_DEFINITIONS,
        )

    async def test_below_or_at_threshold_does_not_request_summary(self):
        history = [{"role": "user", "content": "short task"}]
        size = self.context_size(history)
        with patch("mini_claude.agent.ask_model", new_callable=AsyncMock) as model:
            for threshold in (size, size + 1):
                with self.subTest(threshold=threshold), \
                        patch("mini_claude.agent.CONTEXT_TRIM_TRIGGER_CHARS", threshold):
                    agent = Agent()
                    await agent._maybe_compact_context(history, "short task")
                    self.assertEqual(agent.context_chars, size)
                    self.assertEqual(agent.summary_covered_count, 0)
                    self.assertEqual(agent.summary_messages, [])
                    self.assertEqual(agent.turn_usage.reported_requests, 0)
            model.assert_not_awaited()

    async def test_success_preserves_full_history_and_reuses_or_extends_summary(self):
        original = copy.deepcopy(self.history)

        async def summarize(messages, *, purpose, on_usage):
            self.assertEqual(purpose, "summary")
            on_usage(self.summary_usage)
            return reply("Important decisions and task progress")

        with patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=summarize)) as model:
            await self.agent._maybe_compact_context(self.history, "continue")
            self.assertEqual(self.history, original)
            self.assertEqual(self.agent.messages, original)
            self.assertEqual(load_session(self.path), original)
            self.assertEqual(self.agent.summary_covered_count, len(original))
            first_summary = copy.deepcopy(self.agent.summary_messages)
            self.assertEqual(self.agent._build_context_messages(original), first_summary)
            self.assertLess(self.agent.context_chars, self.context_size(original))
            self.assertLessEqual(self.agent.context_chars, 6000)
            model.assert_awaited_once()
            self.assertEqual(model.call_args.args[0], original)

            # Short new messages reuse the cache without another summary request.
            extended = original + [
                {"role": "assistant", "content": "Done"},
                {"role": "user", "content": "next task"},
            ]
            await self.agent._maybe_compact_context(extended, "next task")
            self.assertEqual(model.await_count, 1)
            self.assertEqual(self.agent.summary_covered_count, len(original))
            self.assertEqual(
                self.agent._build_context_messages(extended),
                first_summary + extended[len(original):],
            )

            # Once new content grows large, summarize the old summary plus the new tail.
            extended.append({"role": "assistant", "content": "New findings " * 2000})
            await self.agent._maybe_compact_context(extended, "next task")
            self.assertEqual(model.await_count, 2)
            self.assertEqual(
                model.call_args.args[0], first_summary + extended[len(original):],
            )
            self.assertEqual(self.agent.summary_covered_count, len(extended))
            self.assertEqual(load_session(self.path), extended)
            self.assertEqual(self.agent.turn_usage.total_tokens, 240)

    async def test_failed_or_cancelled_compaction_keeps_existing_history_and_cache(self):
        cases = ("request_error", "cancel", "empty", "not_shorter", "still_too_large")
        for case in cases:
            with self.subTest(case=case):
                agent = Agent(session_path=self.path)
                agent.messages = copy.deepcopy(self.previous)
                agent.summary_messages = build_summary_messages("Existing summary", "old task")
                agent.summary_covered_count = len(self.previous)
                old_cache = copy.deepcopy(agent.summary_messages)
                save_session(self.path, agent.messages)
                saved_before = self.path.read_bytes()
                history = self.previous + [
                    {"role": "user", "content": "continue"},
                    {"role": "assistant", "content": "New detailed information " * 2000},
                ]
                original = copy.deepcopy(history)

                async def summarize(messages, *, purpose, on_usage):
                    if case in ("request_error", "cancel"):
                        on_usage(None)
                        if case == "cancel":
                            raise asyncio.CancelledError()
                        raise RuntimeError("summary request failed")
                    on_usage(self.summary_usage)
                    return reply({
                        "empty": "", "not_shorter": "x" * 100000,
                        "still_too_large": "x" * 10000,
                    }[case])

                error = (
                    asyncio.CancelledError if case == "cancel"
                    else RuntimeError if case == "request_error" else ValueError
                )
                with patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=summarize)) as model:
                    with self.assertRaises(error):
                        await agent._maybe_compact_context(history, "continue")
                    model.assert_awaited_once()
                self.assertEqual(history, original)
                self.assertEqual(agent.messages, self.previous)
                self.assertEqual(agent.summary_messages, old_cache)
                self.assertEqual(agent.summary_covered_count, len(self.previous))
                self.assertEqual(self.path.read_bytes(), saved_before)
                unknown = case in ("request_error", "cancel")
                self.assertEqual(agent.turn_usage.missing_requests, 1 if unknown else 0)
                self.assertEqual(agent.turn_usage.reported_requests, 0 if unknown else 1)

    async def test_incomplete_tool_batch_is_rejected_before_summary_request(self):
        history = [
            {"role": "user", "content": "task"},
            {
                "role": "assistant", "content": "long findings " * 2000,
                "tool_calls": [{
                    "id": "pending", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
        ]
        with patch("mini_claude.agent.ask_model", new_callable=AsyncMock) as model:
            with self.assertRaises(ValueError):
                await self.agent._maybe_compact_context(history, "task")
            model.assert_not_awaited()
        self.assertEqual(self.agent.messages, self.previous)
        self.assertEqual(self.agent.summary_covered_count, 0)
        self.assertFalse(self.path.exists())

    async def test_chat_counts_summary_and_answer_and_reuses_cache_next_turn(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                agent = Agent(session_path=self.path)
                agent.messages = copy.deepcopy(self.previous)
                calls = []

                async def model(messages, *, purpose="chat", on_usage=None, on_context=None, on_text=None, memory_candidates=None):
                    calls.append((purpose, copy.deepcopy(messages)))
                    if purpose == "summary":
                        on_usage(self.summary_usage)
                        return reply("Task progress and constraints")
                    on_usage(self.chat_usage)
                    on_context(self.context_size(messages))
                    if on_text is not None:
                        on_text("Done")
                    return reply("Done")

                with patch("mini_claude.agent.ask_model", new=model):
                    on_text = (lambda text: None) if streaming else None
                    answer = await agent.chat("continue", on_text=on_text)
                    self.assertEqual(answer, "Done")
                    self.assertEqual([purpose for purpose, _ in calls], ["summary", "chat"])
                    self.assertEqual(agent.turn_usage.reported_requests, 2)
                    self.assertEqual(agent.turn_usage.total_tokens, 350)
                    self.assertEqual(agent.turn_usage.missing_requests, 0)
                    self.assertEqual(agent.summary_covered_count, 3)
                    self.assertEqual(agent.messages[:2], self.previous)
                    self.assertEqual(len(agent.messages), 4)
                    self.assertEqual(load_session(self.path), agent.messages)

                    await agent.chat("next task")
                    self.assertEqual([purpose for purpose, _ in calls], ["summary", "chat", "chat"])
                    self.assertEqual(agent.turn_usage.reported_requests, 1)
                    self.assertEqual(agent.turn_usage.total_tokens, 230)
                    self.assertEqual(agent.summary_covered_count, 3)
                    self.assertEqual(load_session(self.path), agent.messages)

    async def test_large_tool_result_triggers_compaction_between_model_calls(self):
        agent = Agent(session_path=self.path)
        purposes = []
        normal_requests = 0

        async def model(messages, *, purpose="chat", on_usage=None, on_context=None, memory_candidates=None):
            nonlocal normal_requests
            purposes.append(purpose)
            if purpose == "summary":
                validate_tool_pairs(messages)
                on_usage(self.summary_usage)
                return reply("The read succeeded; continue from its findings")
            normal_requests += 1
            on_usage(self.chat_usage)
            if normal_requests == 1:
                return ChatCompletionMessage(
                    role="assistant", content=None,
                    tool_calls=[{
                        "id": "read_1", "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }],
                )
            self.assertEqual(len(messages), 1)
            return reply("Done")

        result = {"is_error": False, "content": "Complete file data " * 2000}
        with patch("mini_claude.agent.ask_model", new=model), \
                patch("mini_claude.agent.execute_tool", return_value=result) as execute:
            self.assertEqual(await agent.chat("Read a file"), "Done")
        execute.assert_called_once()
        self.assertEqual(purposes, ["chat", "summary", "chat"])
        self.assertEqual(agent.summary_covered_count, 3)
        self.assertEqual(agent.turn_usage.reported_requests, 3)
        self.assertEqual(agent.turn_usage.total_tokens, 580)
        self.assertEqual(json.loads(agent.messages[2]["content"]), result)
        self.assertEqual(load_session(self.path), agent.messages)
        validate_tool_pairs(agent.messages)

    async def test_answer_failure_after_compaction_keeps_coverage_consistent(self):
        purposes = []

        async def model(messages, *, purpose="chat", on_usage=None, on_context=None, memory_candidates=None):
            purposes.append(purpose)
            if purpose == "summary":
                on_usage(self.summary_usage)
                return reply("Preserved task progress")
            if purposes.count("chat") == 1:
                raise RuntimeError("answer request failed")
            on_usage(self.chat_usage)
            return reply("Recovered")

        with patch("mini_claude.agent.ask_model", new=model):
            with self.assertRaisesRegex(RuntimeError, "answer request failed"):
                await self.agent.chat("continue")
            self.assertEqual(self.agent.summary_covered_count, 3)
            self.assertEqual(len(self.agent.messages), 3)
            self.assertEqual(load_session(self.path), self.agent.messages)
            self.assertEqual(self.agent.turn_usage.reported_requests, 1)
            self.assertEqual(self.agent.turn_usage.missing_requests, 1)
            self.assertEqual(await self.agent.chat("retry continuation"), "Recovered")
        self.assertEqual(purposes, ["summary", "chat", "chat"])
        self.assertEqual(load_session(self.path), self.agent.messages)

    async def test_summary_failure_does_not_count_a_normal_request_that_never_started(self):
        async def model(messages, *, purpose="chat", on_usage=None):
            self.assertEqual(purpose, "summary")
            on_usage(None)
            raise RuntimeError("summary unavailable")

        with patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=model)) as request:
            with self.assertRaisesRegex(RuntimeError, "summary unavailable"):
                await self.agent.chat("continue")
            request.assert_awaited_once()
        self.assertEqual(self.agent.turn_usage.missing_requests, 1)
        self.assertEqual(self.agent.turn_usage.reported_requests, 0)
        self.assertEqual(self.agent.messages, self.previous)
        self.assertEqual(self.agent.summary_covered_count, 0)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
