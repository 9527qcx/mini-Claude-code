import asyncio
import copy
import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
from openai import APIConnectionError
from openai.types.chat import ChatCompletionChunk, ChatCompletionMessage

from mini_claude.__main__ import main
from mini_claude.agent import Agent
from mini_claude.context import measure_context_chars
from mini_claude.memory import list_memories, read_memory, save_memory
from mini_claude.prompt import build_system_prompt
from mini_claude.session import load_session, save_session, validate_tool_pairs
import mini_claude.client as model_client


def reply(text):
    return ChatCompletionMessage(role="assistant", content=text)


class AgentRecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        workspace = Path.cwd().resolve()
        temporary = TemporaryDirectory(prefix=".agent-recall-check-", dir=workspace)
        self.root = Path(temporary.name).resolve()
        if not self.root.is_relative_to(workspace):
            raise AssertionError("Temporary directory escaped the workspace")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(Path, "cwd", return_value=self.root))
        self.enterContext(patch("mini_claude.prompt.get_git_context", return_value=""))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(redirect_stderr(io.StringIO()))
        self.directory = self.root / ".mini-claude" / "memory"
        self.selection_usage = SimpleNamespace(
            prompt_tokens=100, completion_tokens=10, total_tokens=110,
        )
        self.chat_usage = SimpleNamespace(
            prompt_tokens=200, completion_tokens=20, total_tokens=220,
        )
        self.previous = [
            {"role": "user", "content": "previous task"},
            {"role": "assistant", "content": "previous answer"},
        ]

    async def answer(self, messages, *, on_usage, on_context, **kwargs):
        on_usage(self.chat_usage)
        return reply("normal answer")

    async def test_recall_injects_selected_body_before_user_and_saves_full_history(self):
        memory_id = save_memory(self.directory, title="learning", content="FULL_MEMORY_BODY")
        other_id = save_memory(self.directory, title="unrelated", content="UNSELECTED_BODY")
        path = self.root / "session.json"
        agent = Agent(enable_memory_recall=True, session_path=path)
        agent.messages = copy.deepcopy(self.previous)

        async def choose(messages, *, purpose, on_usage):
            self.assertEqual(purpose, "memory_select")
            payload = json.loads(messages[0]["content"])
            self.assertEqual(payload["query"], "teach me")
            self.assertEqual({item["id"] for item in payload["candidates"]}, {memory_id, other_id})
            on_usage(self.selection_usage)
            return reply(json.dumps({"selected_ids": [memory_id]}))

        with patch("mini_claude.recall.ask_model", new=AsyncMock(side_effect=choose)) as select, \
                patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=self.answer)) as model:
            self.assertEqual(await agent.chat("teach me"), "normal answer")

        select.assert_awaited_once()
        model.assert_awaited_once()
        sent = model.call_args.args[0]
        self.assertEqual(sent[:2], self.previous)
        self.assertIn("FULL_MEMORY_BODY", sent[2]["content"])
        self.assertNotIn("UNSELECTED_BODY", sent[2]["content"])
        self.assertEqual(sent[3], {"role": "user", "content": "teach me"})
        self.assertEqual(agent.messages, sent + [{"role": "assistant", "content": "normal answer"}])
        self.assertEqual(load_session(path), agent.messages)
        self.assertEqual(agent.turn_usage.reported_requests, 2)
        self.assertEqual(agent.turn_usage.missing_requests, 0)
        self.assertEqual(agent.turn_usage.total_tokens, 330)
        self.assertIsNotNone(agent.context_chars)

    async def test_no_candidates_skips_selection_request_and_background(self):
        agent = Agent(enable_memory_recall=True)
        with patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as select, \
                patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=self.answer)) as model:
            await agent.chat("question")
        select.assert_not_awaited()
        self.assertEqual(model.call_args.args[0], [{"role": "user", "content": "question"}])
        self.assertEqual(agent.turn_usage.reported_requests, 1)
        self.assertEqual(agent.turn_usage.missing_requests, 0)
        self.assertFalse(self.directory.exists())

    async def test_optional_recall_is_disabled_for_programmatic_agents_by_default(self):
        agent = Agent()
        with patch("mini_claude.agent.select_relevant_memories", new_callable=AsyncMock) as select, \
                patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=self.answer)):
            await agent.chat("question")
        select.assert_not_awaited()

    async def test_selection_errors_continue_normal_chat_and_keep_actual_usage(self):
        save_memory(self.directory, title="candidate", content="body")
        request = httpx.Request("POST", "https://example.invalid/chat/completions")
        errors = [
            ValueError("malformed selection"),
            httpx.ReadError("stream failed", request=request),
            APIConnectionError(request=request),
        ]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                agent = Agent(enable_memory_recall=True)

                async def fail(messages, *, purpose, on_usage):
                    # Mirror the client's callback: a received invalid response has
                    # usage; an unsuccessful connection has unknown usage.
                    on_usage(self.selection_usage if isinstance(error, ValueError) else None)
                    raise error

                with patch("mini_claude.recall.ask_model", new=AsyncMock(side_effect=fail)), \
                        patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=self.answer)) as model:
                    self.assertEqual(await agent.chat("question"), "normal answer")
                self.assertEqual(model.call_args.args[0], [{"role": "user", "content": "question"}])
                self.assertEqual(len(agent.messages), 2)
                received = isinstance(error, ValueError)
                self.assertEqual(agent.turn_usage.reported_requests, 2 if received else 1)
                self.assertEqual(agent.turn_usage.missing_requests, 0 if received else 1)
                self.assertIn(type(error).__name__, self.errors.getvalue())

    async def test_unusable_memory_directory_does_not_block_prompt_or_chat(self):
        self.directory.parent.mkdir(parents=True)
        self.directory.write_text("a file, not a directory", encoding="utf-8")
        agent = Agent(enable_memory_recall=True)
        with patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as select, \
                patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=self.answer)):
            self.assertEqual(await agent.chat("question"), "normal answer")
        select.assert_not_awaited()
        self.assertIsNotNone(agent.context_chars)
        self.assertEqual(agent.turn_usage.reported_requests, 1)
        self.assertEqual(agent.turn_usage.missing_requests, 0)
        self.assertIn("无法加载启动记忆索引", self.errors.getvalue())
        self.assertEqual(self.errors.getvalue().count("警告："), 1)

    async def test_timeout_cancels_and_finishes_selector_before_normal_request(self):
        agent = Agent(enable_memory_recall=True)
        cleaned = asyncio.Event()
        original_wait_for = asyncio.wait_for

        async def blocked(query, directory, *, candidates, on_usage):
            try:
                await asyncio.Event().wait()
            finally:
                on_usage(None)
                cleaned.set()

        async def short_timeout(awaitable, *, timeout):
            self.assertEqual(timeout, 15)
            return await original_wait_for(awaitable, timeout=0.01)

        async def answer_after_cleanup(messages, **kwargs):
            self.assertTrue(cleaned.is_set())
            return await self.answer(messages, **kwargs)

        with patch("mini_claude.agent.select_relevant_memories", new=blocked), \
                patch("mini_claude.agent.asyncio.wait_for", new=short_timeout), \
                patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=answer_after_cleanup)) as model:
            self.assertEqual(await agent.chat("question"), "normal answer")
        model.assert_awaited_once()
        self.assertEqual(agent.turn_usage.reported_requests, 1)
        self.assertEqual(agent.turn_usage.missing_requests, 1)
        self.assertIn("TimeoutError", self.errors.getvalue())

    async def test_user_cancellation_propagates_without_starting_chat_or_saving(self):
        path = self.root / "session.json"
        agent = Agent(enable_memory_recall=True, session_path=path)
        agent.messages = copy.deepcopy(self.previous)
        entered = asyncio.Event()
        cleaned = asyncio.Event()

        async def blocked(query, directory, *, candidates, on_usage):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                on_usage(None)
                cleaned.set()

        with patch("mini_claude.agent.select_relevant_memories", new=blocked), \
                patch("mini_claude.agent.ask_model", new_callable=AsyncMock) as model:
            task = asyncio.create_task(agent.chat("question"))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cleaned.is_set())
        model.assert_not_awaited()
        self.assertEqual(agent.messages, self.previous)
        self.assertFalse(path.exists())
        self.assertEqual(agent.turn_usage.reported_requests, 0)
        self.assertEqual(agent.turn_usage.missing_requests, 1)
        self.assertNotIn("记忆召回失败", self.errors.getvalue())

    async def test_startup_index_is_reused_and_only_new_agent_sees_new_records(self):
        old_id = save_memory(self.directory, title="old record", content="old body")
        seen_indices = []

        async def choose(messages, **kwargs):
            payload = json.loads(messages[0]["content"])
            seen_indices.append({item["id"] for item in payload["candidates"]})
            # An ID absent from the startup index must be rejected even if
            # the selection model happens to return it.
            return reply(json.dumps({"selected_ids": [old_id, new_id]}))

        with patch("mini_claude.agent.list_memories", wraps=list_memories) as scan:
            agent = Agent(enable_memory_recall=True)
            self.assertEqual(scan.call_count, 1)
            snapshot = copy.deepcopy(agent.memory_candidates)
            self.assertEqual(set(snapshot[0]), {"id", "title", "created_at"})
            new_id = save_memory(self.directory, title="new record", content="new body")
            with patch("mini_claude.recall.list_memories", side_effect=AssertionError("unexpected rescan")), \
                    patch("mini_claude.recall.ask_model", new=AsyncMock(side_effect=choose)):
                for query in ("first question", "different question"):
                    background = await agent._recall_memory(query)
                    records = json.loads(background["content"].split("\n\n", 1)[1])
                    self.assertEqual([item["id"] for item in records], [old_id])
                self.assertEqual(agent.memory_candidates, snapshot)
                self.assertEqual(scan.call_count, 1)
                fresh = Agent(enable_memory_recall=True)
                self.assertEqual(scan.call_count, 2)
                background = await fresh._recall_memory("new session")
                records = json.loads(background["content"].split("\n\n", 1)[1])
                self.assertEqual([item["id"] for item in records], [old_id, new_id])
        self.assertEqual(seen_indices, [{old_id}, {old_id}, {old_id, new_id}])

    async def test_empty_startup_index_stays_empty_after_new_record_is_saved(self):
        agent = Agent(enable_memory_recall=True)
        self.assertEqual(agent.memory_candidates, [])
        memory_id = save_memory(self.directory, title="added later", content="new body")
        with patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as select, \
                patch("mini_claude.recall.list_memories", side_effect=AssertionError("unexpected rescan")):
            self.assertIsNone(await agent._recall_memory("question"))
            select.assert_not_awaited()
        self.assertEqual(agent.memory_candidates, [])
        fresh = Agent(enable_memory_recall=True)
        self.assertEqual([item["id"] for item in fresh.memory_candidates], [memory_id])

    async def test_startup_load_failure_keeps_empty_index_after_directory_is_repaired(self):
        with patch("mini_claude.agent.list_memories", side_effect=OSError("unavailable")) as scan:
            agent = Agent(enable_memory_recall=True)
        scan.assert_called_once_with(self.directory)
        save_memory(self.directory, title="now accessible", content="body")
        with patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as select:
            self.assertIsNone(await agent._recall_memory("question"))
        select.assert_not_awaited()
        self.assertEqual(agent.memory_candidates, [])
        self.assertIn("无法加载启动记忆索引", self.errors.getvalue())

    async def test_clear_and_restore_keep_original_startup_index(self):
        memory_id = save_memory(self.directory, title="initial", content="body")
        agent = Agent(enable_memory_recall=True)
        snapshot = agent.memory_candidates
        save_memory(self.directory, title="later", content="another body")
        path = self.root / "restore.json"
        save_session(path, self.previous)
        with patch("mini_claude.agent.list_memories", side_effect=AssertionError("unexpected rescan")):
            agent.clear_history()
            agent.restore_session(path)
        self.assertIs(agent.memory_candidates, snapshot)
        self.assertEqual([item["id"] for item in snapshot], [memory_id])
        self.assertEqual(agent.messages, self.previous)

    async def test_preflight_and_actual_request_use_startup_index_without_rescanning(self):
        for initially_empty in (False, True):
            for streaming in (False, True):
                with self.subTest(initially_empty=initially_empty, streaming=streaming):
                    project = self.root / f"snapshot-{initially_empty}-{streaming}"
                    project.mkdir()
                    directory = project / ".mini-claude" / "memory"
                    old_id = None
                    if not initially_empty:
                        old_id = save_memory(directory, title="STARTUP_TITLE", content="BODY_NOT_IN_INDEX")
                    with patch.object(Path, "cwd", return_value=project):
                        agent = Agent()
                        original = copy.deepcopy(agent.memory_candidates)
                        new_id = save_memory(directory, title="ADDED_AFTER_STARTUP", content="NEW_BODY")
                        sdk_client = MagicMock()
                        sdk_client.__aenter__ = AsyncMock(return_value=sdk_client)
                        sdk_client.__aexit__ = AsyncMock(return_value=False)
                        stream = MagicMock()
                        stream.__aenter__ = AsyncMock(return_value=stream)
                        stream.__aexit__ = AsyncMock(return_value=False)
                        stream.__aiter__.return_value = [ChatCompletionChunk(
                            id="snapshot_test", created=0, model="test", object="chat.completion.chunk",
                            choices=[{
                                "index": 0, "delta": {"content": "answer"}, "finish_reason": "stop",
                            }],
                        )]
                        sdk_client.chat.completions.create = AsyncMock(return_value=stream)
                        with patch.object(model_client, "load_dotenv"), \
                                patch.object(model_client, "AsyncOpenAI", return_value=sdk_client), \
                                patch.dict(os.environ, {
                                    "OPENAI_API_KEY": "test-only",
                                    "OPENAI_API_BASE": "https://example.invalid/v1",
                                    "OPENAI_MODEL": "test",
                                }), \
                                patch("mini_claude.memory.list_memories", side_effect=AssertionError("unexpected rescan")), \
                                patch("mini_claude.agent.build_system_prompt", wraps=build_system_prompt) as prompt, \
                                patch("mini_claude.agent.measure_context_chars", wraps=measure_context_chars) as measure:
                            display = Mock() if streaming else None
                            self.assertEqual(await agent.chat("question", on_text=display), "answer")
                        sdk_client.chat.completions.create.assert_awaited_once()
                        sent = sdk_client.chat.completions.create.call_args.kwargs["messages"]
                        prompt.assert_called_once_with(memory_candidates=agent.memory_candidates)
                        preflight_system = measure.call_args.args[0][0]
                        self.assertEqual(sent[0], preflight_system)
                        system_text = sent[0]["content"]
                        self.assertNotIn(new_id, system_text)
                        self.assertNotIn("ADDED_AFTER_STARTUP", system_text)
                        self.assertNotIn("BODY_NOT_IN_INDEX", system_text)
                        self.assertNotIn("NEW_BODY", system_text)
                        if old_id is not None:
                            self.assertIn(old_id, system_text)
                            self.assertIn("STARTUP_TITLE", system_text)
                        else:
                            self.assertNotIn("长期记忆索引", system_text)
                        self.assertEqual(agent.memory_candidates, original)
                        if display is not None:
                            display.assert_any_call("answer")
                        fresh = Agent()
                        self.assertIn(new_id, {item["id"] for item in fresh.memory_candidates})

    async def test_recall_runs_once_per_user_message_and_not_per_tool_iteration(self):
        memory_id = save_memory(self.directory, title="learning", content="remember this")
        memory = read_memory(self.directory, memory_id)
        agent = Agent(enable_memory_recall=True)
        tool_reply = ChatCompletionMessage(
            role="assistant", content=None,
            tool_calls=[{
                "id": "read_one", "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path":"fixture.txt"}'},
            }],
        )
        with patch("mini_claude.agent.select_relevant_memories", new=AsyncMock(return_value=[memory])) as select, \
                patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=[tool_reply, reply("done"), reply("next")])) as model, \
                patch("mini_claude.agent.execute_tool", return_value={"is_error": False, "content": "fixture"}) as execute:
            self.assertEqual(await agent.chat("first"), "done")
            select.assert_awaited_once()
            self.assertEqual(model.await_count, 2)
            execute.assert_called_once()
            validate_tool_pairs(agent.messages)
            self.assertEqual(await agent.chat("second"), "next")
            self.assertEqual(select.await_count, 2)
            self.assertEqual([call.args[0] for call in select.call_args_list], ["first", "second"])


class RecallEntryTests(unittest.TestCase):
    def test_cli_explicitly_enables_recall(self):
        with patch("sys.argv", ["mini_claude", "question"]), \
                patch("mini_claude.__main__.Agent") as factory, \
                patch("mini_claude.__main__.show_answer", new_callable=AsyncMock) as show, \
                redirect_stdout(io.StringIO()):
            main()
        self.assertTrue(factory.call_args.kwargs["enable_memory_recall"])
        show.assert_awaited_once_with(factory.return_value, "question")


if __name__ == "__main__":
    unittest.main()
