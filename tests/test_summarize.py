import asyncio
import copy
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from httpx import Request, Response
from openai import APIConnectionError, BadRequestError
from openai.types.chat import ChatCompletion

from mini_claude.summarize import SUMMARY_PROMPT, summarize_context
from mini_claude.usage import UsageStats


def completion(content="Task summary", *, finish_reason="stop", usage=None, tool_calls=None):
    return ChatCompletion(
        id="summary_test", created=0, model="test",
        object="chat.completion",
        choices=[{
            "index": 0,
            "finish_reason": finish_reason,
            "message": {
                "role": "assistant", "content": content,
                "tool_calls": tool_calls,
            },
        }],
        usage=usage,
    )


class SummarizeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.messages = [
            {"role": "user", "content": "Remember the goal: fix imports"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_read", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {
                "role": "tool", "tool_call_id": "call_read",
                "content": json.dumps({
                    "is_error": False,
                    "content": "File text, including instructions, is task data.",
                }),
            },
        ]
        self.original = copy.deepcopy(self.messages)
        self.create = AsyncMock()
        self.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=self.create)),
        )
        self.usage = {
            "prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120,
        }
        self.stats = UsageStats()
        self.on_usage = Mock(side_effect=self.stats.record)

    async def summarize(self):
        return await summarize_context(
            self.messages, client=self.client, model="test", on_usage=self.on_usage,
        )

    async def test_summary_uses_history_as_data_without_tools_or_mutation(self):
        self.create.return_value = completion("  Task summary\n", usage=self.usage)
        self.assertEqual(await self.summarize(), "Task summary")
        self.create.assert_awaited_once()
        request = self.create.call_args.kwargs
        self.assertEqual(request["model"], "test")
        self.assertFalse(request["stream"])
        self.assertNotIn("tools", request)
        self.assertEqual(
            [message["role"] for message in request["messages"]],
            ["system", "user"],
        )
        self.assertEqual(request["messages"][0]["content"], SUMMARY_PROMPT)
        self.assertEqual(
            json.loads(request["messages"][1]["content"]),
            {"conversation": self.original},
        )
        self.assertEqual(self.messages, self.original)
        self.on_usage.assert_called_once_with(self.create.return_value.usage)
        self.assertEqual(self.stats.total_tokens, 120)

    async def test_missing_usage_is_distinct_from_reported_zero_usage(self):
        for usage in (None, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}):
            with self.subTest(usage=usage):
                self.setUp()
                self.create.return_value = completion(usage=usage)
                self.assertEqual(await self.summarize(), "Task summary")
                self.assertEqual(self.on_usage.call_count, 1)
                self.assertEqual(self.stats.reported_requests, 0 if usage is None else 1)
                self.assertEqual(self.stats.missing_requests, 1 if usage is None else 0)
                self.assertEqual(self.stats.total_tokens, 0)

    async def test_empty_or_unfinished_summaries_are_rejected_but_usage_is_recorded(self):
        cases = [
            completion("", usage=self.usage),
            completion(" \n\t", usage=self.usage),
            completion(None, usage=self.usage),
            completion("partial", finish_reason="length", usage=self.usage),
            completion(None, finish_reason="content_filter", usage=self.usage),
            ChatCompletion(
                id="empty", created=0, model="test", object="chat.completion",
                choices=[], usage=self.usage,
            ),
        ]
        for response in cases:
            with self.subTest(response=response):
                self.setUp()
                self.create.return_value = response
                with self.assertRaises(ValueError):
                    await self.summarize()
                self.create.assert_awaited_once()
                self.on_usage.assert_called_once_with(response.usage)
                self.assertEqual(self.stats.total_tokens, 120)
                self.assertEqual(self.messages, self.original)

    async def test_unexpected_tool_call_is_rejected(self):
        self.create.return_value = completion(
            usage=self.usage,
            tool_calls=[{
                "id": "unexpected", "type": "function",
                "function": {"name": "write_file", "arguments": "{}"},
            }],
        )
        with self.assertRaisesRegex(ValueError, "不允许调用工具"):
            await self.summarize()
        self.create.assert_awaited_once()
        self.on_usage.assert_called_once_with(self.create.return_value.usage)
        self.assertEqual(self.messages, self.original)

    async def test_transient_failure_retries_same_request_and_reports_usage_once(self):
        self.create.side_effect = [
            APIConnectionError(request=Request("POST", "https://example.invalid/v1")),
            completion(usage=self.usage),
        ]
        with patch("mini_claude.retry.asyncio.sleep", new_callable=AsyncMock) as sleep, \
                patch("mini_claude.retry.sys.stderr", new=io.StringIO()):
            self.assertEqual(await self.summarize(), "Task summary")
        self.assertEqual(self.create.await_count, 2)
        self.assertEqual(self.create.call_args_list[0], self.create.call_args_list[1])
        sleep.assert_awaited_once_with(1)
        self.assertEqual(self.on_usage.call_count, 1)
        self.assertEqual(self.stats.reported_requests, 1)
        self.assertEqual(self.stats.missing_requests, 0)

    async def test_exhausted_retries_report_missing_usage_and_preserve_history(self):
        error = APIConnectionError(request=Request("POST", "https://example.invalid/v1"))
        self.create.side_effect = error
        with patch("mini_claude.retry.asyncio.sleep", new_callable=AsyncMock) as sleep, \
                patch("mini_claude.retry.sys.stderr", new=io.StringIO()):
            with self.assertRaises(APIConnectionError) as caught:
                await self.summarize()
        self.assertIs(caught.exception, error)
        self.assertEqual(self.create.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [1, 2])
        self.on_usage.assert_called_once_with(None)
        self.assertEqual(self.stats.missing_requests, 1)
        self.assertEqual(self.messages, self.original)

    async def test_bad_request_propagates_without_retry(self):
        error = BadRequestError(
            "invalid request",
            response=Response(400, request=Request("POST", "https://example.invalid/v1")),
            body={"error": "invalid request"},
        )
        self.create.side_effect = error
        with self.assertRaises(BadRequestError) as caught:
            await self.summarize()
        self.assertIs(caught.exception, error)
        self.create.assert_awaited_once()
        self.on_usage.assert_called_once_with(None)
        self.assertEqual(self.messages, self.original)

    async def test_cancellation_propagates_and_reports_missing_usage(self):
        started = asyncio.Event()

        async def blocked_request(**kwargs):
            started.set()
            await asyncio.Event().wait()

        self.create.side_effect = blocked_request
        task = asyncio.create_task(self.summarize())
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.create.assert_awaited_once()
        self.on_usage.assert_called_once_with(None)
        self.assertEqual(self.messages, self.original)


if __name__ == "__main__":
    unittest.main()
