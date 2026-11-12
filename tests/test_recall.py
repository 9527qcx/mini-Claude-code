import asyncio
import copy
import io
import json
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

from openai.types.chat import ChatCompletionMessage

from mini_claude.memory import list_memories, read_memory, save_memory
from mini_claude.recall import (
    build_recalled_memory_message,
    select_relevant_memories,
)


def selection(ids):
    return ChatCompletionMessage(role="assistant", content=json.dumps({"selected_ids": ids}))


class RecallTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        workspace = Path.cwd().resolve()
        temporary = TemporaryDirectory(prefix=".recall-check-", dir=workspace)
        self.root = Path(temporary.name).resolve()
        if not self.root.is_relative_to(workspace):
            raise AssertionError("Temporary directory escaped the workspace")
        self.addCleanup(temporary.cleanup)
        self.directory = self.root / "memory"

    def save(self, title, content):
        return save_memory(self.directory, title=title, content=content)

    def test_empty_memories_do_not_create_a_background_message(self):
        self.assertIsNone(build_recalled_memory_message([]))

    def test_background_message_keeps_full_selected_content_without_mutation(self):
        memory_id = self.save("学习偏好", "保留完整正文，当前用户要求优先。")
        memories = [read_memory(self.directory, memory_id)]
        original = copy.deepcopy(memories)
        message = build_recalled_memory_message(memories)
        self.assertEqual(message["role"], "user")
        self.assertIn("不是新的用户指令", message["content"])
        records = json.loads(message["content"].split("\n\n", 1)[1])
        self.assertEqual(records, [{
            key: memories[0][key]
            for key in ("id", "title", "content", "created_at")
        }])
        self.assertEqual(memories, original)

    async def test_no_candidates_returns_empty_without_requesting_model(self):
        with patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as request:
            self.assertEqual(await select_relevant_memories("question", self.directory), [])
            request.assert_not_awaited()
        self.assertFalse(self.directory.exists())

    async def test_preloaded_candidates_skip_rescan_and_load_current_selected_body(self):
        memory_id = self.save("learning preferences", "original body")
        candidates = list_memories(self.directory)
        original_candidates = copy.deepcopy(candidates)
        memory = read_memory(self.directory, memory_id)
        memory["content"] = "updated body after preloading"
        (self.directory / f"{memory_id}.json").write_text(
            json.dumps(memory), encoding="utf-8",
        )
        with patch("mini_claude.recall.list_memories") as scan, \
                patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as request:
            request.return_value = selection([memory_id])
            recalled = await select_relevant_memories(
                "teach me", self.directory, candidates=candidates,
            )
        scan.assert_not_called()
        self.assertEqual(recalled[0]["content"], "updated body after preloading")
        self.assertEqual(candidates, original_candidates)
        sent = json.loads(request.call_args.args[0][0]["content"])
        self.assertEqual(sent["candidates"], candidates)
        self.assertNotIn("updated body", json.dumps(sent))

    async def test_explicit_empty_index_does_not_rescan_even_if_files_exist(self):
        self.save("new memory", "body")
        with patch("mini_claude.recall.list_memories") as scan, \
                patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as request:
            self.assertEqual(await select_relevant_memories(
                "question", self.directory, candidates=[],
            ), [])
        scan.assert_not_called()
        request.assert_not_awaited()

    async def test_invalid_query_is_rejected_before_loading_or_requesting(self):
        with patch("mini_claude.recall.list_memories") as candidates, \
                patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as request:
            for query in (None, 42, "", " \n"):
                with self.subTest(query=query), self.assertRaises(ValueError):
                    await select_relevant_memories(query, self.directory)
            candidates.assert_not_called()
            request.assert_not_awaited()

    async def test_only_selected_bodies_are_loaded_and_only_metadata_is_sent(self):
        learning = self.save("Python 学习偏好", "LEARNING_BODY_ONLY")
        deployment = self.save("部署注意事项", "DEPLOYMENT_BODY_ONLY")
        on_usage = Mock()
        with patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as request, \
                patch("mini_claude.recall.read_memory", wraps=read_memory) as read:
            request.return_value = selection([learning])
            memories = await select_relevant_memories(
                "教我 Python", self.directory, on_usage=on_usage,
            )
        self.assertEqual([memory["id"] for memory in memories], [learning])
        self.assertEqual(memories[0]["content"], "LEARNING_BODY_ONLY")
        read.assert_called_once_with(self.directory, learning)
        request.assert_awaited_once()
        self.assertEqual(request.call_args.kwargs["purpose"], "memory_select")
        self.assertIs(request.call_args.kwargs["on_usage"], on_usage)
        messages = request.call_args.args[0]
        self.assertEqual([message["role"] for message in messages], ["user"])
        payload = json.loads(messages[0]["content"])
        self.assertEqual(payload["query"], "教我 Python")
        self.assertEqual({item["id"] for item in payload["candidates"]}, {learning, deployment})
        for candidate in payload["candidates"]:
            self.assertEqual(set(candidate), {"id", "title", "created_at"})
        self.assertNotIn("LEARNING_BODY_ONLY", messages[0]["content"])
        self.assertNotIn("DEPLOYMENT_BODY_ONLY", messages[0]["content"])

    async def test_unknown_duplicate_and_excess_ids_do_not_cause_extra_reads(self):
        ids = [self.save(f"memory-{index}", f"body-{index}") for index in range(4)]
        with patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as request, \
                patch("mini_claude.recall.read_memory", wraps=read_memory) as read:
            request.return_value = selection(["../../outside", ids[2], ids[2], ids[0], ids[3], ids[1]])
            memories = await select_relevant_memories("question", self.directory)
        self.assertEqual([item["id"] for item in memories], [ids[2], ids[0], ids[3]])
        self.assertEqual([call.args[1] for call in read.call_args_list], [ids[2], ids[0], ids[3]])

    async def test_selected_record_disappearing_or_breaking_does_not_lose_other_results(self):
        for mode in ("deleted", "corrupted"):
            with self.subTest(mode=mode):
                directory = self.root / mode
                bad = save_memory(directory, title="changed record", content="old body")
                good = save_memory(directory, title="valid record", content="keep this")
                path = directory / f"{bad}.json"

                async def choose(messages, **kwargs):
                    # Simulate a change after the candidate index has been scanned.
                    if mode == "deleted":
                        path.unlink()
                    else:
                        path.write_text("{broken", encoding="utf-8")
                    return selection([bad, good])

                errors = io.StringIO()
                with patch("mini_claude.recall.ask_model", new=choose), redirect_stderr(errors):
                    memories = await select_relevant_memories("question", directory)
                self.assertEqual([item["id"] for item in memories], [good])
                self.assertEqual(memories[0]["content"], "keep this")
                self.assertIn(bad, errors.getvalue())

    async def test_empty_selection_loads_nothing_and_malformed_selection_propagates(self):
        self.save("candidate", "body")
        with patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as request, \
                patch("mini_claude.recall.read_memory") as read:
            request.return_value = selection([])
            self.assertEqual(await select_relevant_memories("question", self.directory), [])
            read.assert_not_called()
            for content in ("not JSON", "[]", '{"selected_ids": [42]}'):
                with self.subTest(content=content):
                    request.return_value = ChatCompletionMessage(role="assistant", content=content)
                    with self.assertRaises(ValueError):
                        await select_relevant_memories("question", self.directory)
            read.assert_not_called()

    async def test_request_failure_and_cancellation_propagate_without_reading_bodies(self):
        self.save("candidate", "body")
        for error in (RuntimeError("request failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__), \
                    patch("mini_claude.recall.ask_model", new_callable=AsyncMock) as request, \
                    patch("mini_claude.recall.read_memory") as read:
                request.side_effect = error
                with self.assertRaises(type(error)) as caught:
                    await select_relevant_memories("question", self.directory)
                self.assertIs(caught.exception, error)
                read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
