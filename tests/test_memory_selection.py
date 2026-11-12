import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from openai.types.chat import ChatCompletion

import mini_claude.client as model_client
from mini_claude.memory import MEMORY_SELECTION_PROMPT, parse_memory_selection


class MemorySelectionTests(unittest.TestCase):
    def setUp(self):
        self.ids = [character * 32 for character in "abcde"]
        self.candidates = [
            {"id": memory_id, "title": f"memory-{index}"}
            for index, memory_id in enumerate(self.ids)
        ]

    def test_selection_filters_unknown_ids_and_duplicates_preserving_rank(self):
        selected = [self.ids[2], "../../outside", self.ids[2], self.ids[0]]
        result = parse_memory_selection(
            json.dumps({"selected_ids": selected}), self.candidates,
        )
        self.assertEqual(result, [self.ids[2], self.ids[0]])

    def test_result_limit_and_empty_selection(self):
        text = json.dumps({"selected_ids": list(reversed(self.ids))})
        self.assertEqual(parse_memory_selection(text, self.candidates), self.ids[::-1][:3])
        self.assertEqual(parse_memory_selection(text, self.candidates, limit=1), [self.ids[-1]])
        self.assertEqual(parse_memory_selection(text, []), [])
        self.assertEqual(parse_memory_selection('{"selected_ids": []}', self.candidates), [])

    def test_invalid_json_or_schema_is_rejected(self):
        for text in (
            None, 42, "{broken", "null", "[]", "{}",
            '{"selected_ids": "id"}', '{"selected_ids": null}',
            '{"selected_ids": [42]}', '{"selected_ids": [null]}',
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_memory_selection(text, self.candidates)

    def test_nonpositive_limit_is_rejected(self):
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                parse_memory_selection('{"selected_ids": []}', self.candidates, limit=limit)


class MemorySelectionRequestTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.__aenter__ = AsyncMock(return_value=self.client)
        self.client.__aexit__ = AsyncMock(return_value=None)
        self.client.chat.completions.create = AsyncMock()
        self.enterContext(patch.object(model_client, "load_dotenv"))
        self.enterContext(patch.object(model_client, "AsyncOpenAI", return_value=self.client))
        self.project_prompt = self.enterContext(patch.object(model_client, "build_system_prompt"))
        self.enterContext(patch.dict(os.environ, {
            "OPENAI_API_KEY": "test-only",
            "OPENAI_API_BASE": "https://example.invalid/v1",
            "OPENAI_MODEL": "test",
        }))
        self.messages = [{
            "role": "user",
            "content": json.dumps({
                "query": "Explain generators",
                "candidates": [{"id": "a" * 32, "title": "Python learning preferences"}],
            }),
        }]

    def completion(self, *, content='{"selected_ids": []}', reason="stop", calls=None):
        return ChatCompletion(
            id="selection_test", created=0, model="test", object="chat.completion",
            choices=[{
                "index": 0, "finish_reason": reason,
                "message": {"role": "assistant", "content": content, "tool_calls": calls},
            }],
            usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        )

    async def test_selection_uses_own_prompt_without_tools_or_streaming_callbacks(self):
        response = self.completion(content=json.dumps({"selected_ids": ["a" * 32]}))
        self.client.chat.completions.create.return_value = response
        on_usage, on_text, on_context = Mock(), Mock(), Mock()
        answer = await model_client.ask_model(
            self.messages, purpose="memory_select", on_usage=on_usage,
            on_text=on_text, on_context=on_context,
        )
        self.assertEqual(parse_memory_selection(answer.content, [{"id": "a" * 32}]), ["a" * 32])
        kwargs = self.client.chat.completions.create.call_args.kwargs
        self.assertEqual(kwargs["messages"], [
            {"role": "system", "content": MEMORY_SELECTION_PROMPT}, *self.messages,
        ])
        self.assertFalse(kwargs["stream"])
        self.assertNotIn("tools", kwargs)
        self.assertEqual(kwargs["model"], "test")
        self.project_prompt.assert_not_called()
        on_usage.assert_called_once_with(response.usage)
        on_text.assert_not_called()
        on_context.assert_not_called()
        self.client.__aexit__.assert_awaited_once()

    async def test_incomplete_empty_or_tool_responses_fail_after_recording_usage(self):
        responses = [
            self.completion(content="partial", reason="length"),
            self.completion(content=None),
            self.completion(content=" \n"),
            self.completion(calls=[{
                "id": "unexpected", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }]),
            ChatCompletion(
                id="empty", created=0, model="test", object="chat.completion", choices=[],
                usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            ),
        ]
        for response in responses:
            with self.subTest(response=response):
                self.client.reset_mock()
                self.client.chat.completions.create.return_value = response
                on_usage = Mock()
                with self.assertRaises(ValueError):
                    await model_client.ask_model(self.messages, purpose="memory_select", on_usage=on_usage)
                on_usage.assert_called_once_with(response.usage)
                self.client.__aexit__.assert_awaited_once()

    async def test_request_failure_or_cancellation_reports_unknown_usage_and_propagates(self):
        for error in (RuntimeError("request failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.client.reset_mock()
                self.client.chat.completions.create.side_effect = error
                on_usage = Mock()
                with self.assertRaises(type(error)):
                    await model_client.ask_model(self.messages, purpose="memory_select", on_usage=on_usage)
                self.client.chat.completions.create.assert_awaited_once()
                on_usage.assert_called_once_with(None)
                self.client.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
