import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from httpx import Request
from openai import APIConnectionError
from openai.types.chat import ChatCompletion, ChatCompletionChunk, ChatCompletionMessage

import mini_claude.__main__ as cli
import mini_claude.client as model_client
from mini_claude.agent import Agent
from mini_claude.context import measure_context_chars
from mini_claude.summarize import SUMMARY_PROMPT


def chunk(content=None, tool_calls=None, finish_reason=None):
    return ChatCompletionChunk(
        id="completion_test",
        created=0,
        model="test",
        object="chat.completion.chunk",
        choices=[{
            "index": 0,
            "delta": {"content": content, "tool_calls": tool_calls},
            "finish_reason": finish_reason,
        }],
    )


class FakeStream:
    def __init__(self, chunks, owner=None):
        self.chunks = chunks
        self.owner = owner
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def __aiter__(self):
        for item in self.chunks:
            if self.owner is not None and self.owner.closed:
                raise RuntimeError("Client closed before stream was consumed")
            yield item


class FakeClient:
    def __init__(self):
        self.closed = False
        self.stream = FakeStream([
            chunk(content="Hello"),
            chunk(content=" world"),
            chunk(finish_reason="stop"),
        ], owner=self)
        self.create = AsyncMock(return_value=self.stream)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True


class StreamingTests(unittest.TestCase):
    def test_console_displays_fragments_before_completion_and_closes_lines_on_error(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                output = io.StringIO()
                test = self

                class ProgressStream(FakeStream):
                    async def __aiter__(self):
                        yield chunk(content="Hello")
                        test.assertEqual(output.getvalue(), "助手：Hello")
                        if fail:
                            raise RuntimeError("simulated stream failure")
                        yield chunk(content=" world")
                        test.assertEqual(output.getvalue(), "助手：Hello world")
                        yield chunk(finish_reason="stop")

                client = FakeClient()
                client.stream = ProgressStream([], owner=client)
                client.create.return_value = client.stream
                agent = Agent()
                with patch.object(model_client, "load_dotenv"), \
                        patch.object(model_client, "build_system_prompt", return_value="test"), \
                        patch.object(model_client, "AsyncOpenAI", return_value=client), \
                        patch.dict(os.environ, {
                            "OPENAI_API_KEY": "test-only",
                            "OPENAI_API_BASE": "https://example.invalid/v1",
                            "OPENAI_MODEL": "test",
                        }), redirect_stdout(output):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "simulated"):
                            asyncio.run(cli.show_answer(agent, "hello"))
                    else:
                        asyncio.run(cli.show_answer(agent, "hello"))

                if fail:
                    self.assertEqual(
                        output.getvalue(),
                        "助手：Hello\n"
                        "本轮用量未知：1 次调用未返回统计。\n"
                        f"最近准备请求的上下文：{agent.context_chars} 字符（非 token 数）\n"
                        "本轮未完成，用量统计可能不完整。\n",
                    )
                    self.assertEqual(agent.messages, [])
                else:
                    self.assertEqual(
                        output.getvalue(),
                        "助手：Hello world\n"
                        "本轮用量未知：1 次调用未返回统计。\n"
                        f"最近准备请求的上下文：{agent.context_chars} 字符（非 token 数）\n",
                    )
                    self.assertEqual(agent.messages[-1]["content"], "Hello world")
                self.assertEqual(agent.turn_usage.reported_requests, 0)
                self.assertEqual(agent.turn_usage.missing_requests, 1)
                self.assertIsInstance(agent.context_chars, int)
                self.assertGreater(agent.context_chars, 0)
                self.assertTrue(client.stream.closed)
                self.assertTrue(client.closed)

    def test_console_reports_usage_from_final_empty_choice_chunk(self):
        client = FakeClient()
        client.stream.chunks.append(ChatCompletionChunk(
            id="completion_test", created=0, model="test",
            object="chat.completion.chunk", choices=[],
            usage={
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        ))
        output = io.StringIO()
        agent = Agent()
        with patch.object(model_client, "load_dotenv"), \
                patch.object(model_client, "build_system_prompt", return_value="test"), \
                patch.object(model_client, "AsyncOpenAI", return_value=client), \
                patch.dict(os.environ, {
                    "OPENAI_API_KEY": "test-only",
                    "OPENAI_API_BASE": "https://example.invalid/v1",
                    "OPENAI_MODEL": "test",
                }), redirect_stdout(output):
            asyncio.run(cli.show_answer(agent, "hello"))

        self.assertEqual(
            output.getvalue(),
            "助手：Hello world\n"
            "本轮已报告用量：输入 100，输出 20，合计 120 token\n"
            "有统计的调用：1，缺少统计的调用：0\n"
            f"最近准备请求的上下文：{agent.context_chars} 字符（非 token 数）\n",
        )
        self.assertEqual(agent.turn_usage.total_tokens, 120)
        self.assertNotIn("usage", agent.messages[-1])
        self.assertTrue(client.stream.closed)
        self.assertTrue(client.closed)

    def test_tool_logs_and_turn_limit_notice_remain_visible_without_duplicate_text(self):
        tool_request = ChatCompletionMessage(
            role="assistant", content="Reading",
            tool_calls=[{
                "id": "call_read", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        )
        final = ChatCompletionMessage(role="assistant", content="Done")
        for max_turns in (1, 2):
            with self.subTest(max_turns=max_turns):
                responses = iter([tool_request, final])

                async def model(messages, *, on_text, on_usage=None, on_context=None):
                    response = next(responses)
                    on_context(1000 if response.tool_calls else 2000)
                    on_text(response.content)
                    if on_usage is not None:
                        on_usage(None)
                    return response

                output = io.StringIO()
                agent = Agent(max_turns=max_turns)
                with patch("mini_claude.agent.ask_model", new=model), \
                        patch("mini_claude.agent.execute_tool", return_value={
                            "is_error": False, "content": "file contents",
                        }) as execute, redirect_stdout(output):
                    asyncio.run(cli.show_answer(agent, "read"))
                execute.assert_called_once()
                prefix = "助手：Reading\n[工具调用] read_file\n[工具结果] 成功\n"
                ending = agent.messages[-1]["content"] + "\n"
                summary = f"本轮用量未知：{max_turns} 次调用未返回统计。\n"
                expected_context = 1000 if max_turns == 1 else 2000
                summary += (
                    f"最近准备请求的上下文：{expected_context} 字符（非 token 数）\n"
                )
                self.assertEqual(output.getvalue(), prefix + ending + summary)
                self.assertEqual(agent.context_chars, expected_context)
                self.assertEqual(agent.turn_usage.missing_requests, max_turns)
                if max_turns == 2:
                    self.assertEqual(ending, "Done\n")
                else:
                    self.assertIn("上限", ending)

    def test_client_stays_open_until_stream_is_consumed(self):
        client = FakeClient()
        with patch.object(model_client, "load_dotenv"), \
                patch.object(model_client, "build_system_prompt", return_value="test"), \
                patch.object(model_client, "AsyncOpenAI", return_value=client), \
                patch.dict(os.environ, {
                    "OPENAI_API_KEY": "test-only",
                    "OPENAI_API_BASE": "https://example.invalid/v1",
                    "OPENAI_MODEL": "test",
                }):
            result = asyncio.run(model_client.ask_model([
                {"role": "user", "content": "hello"},
            ]))
        self.assertEqual(result.content, "Hello world")
        self.assertTrue(client.stream.closed)
        self.assertTrue(client.closed)
        self.assertTrue(client.create.call_args.kwargs["stream"])

    def test_context_measures_complete_request_once_across_retries(self):
        client = FakeClient()
        on_context = Mock()
        history = [{"role": "user", "content": "hello"}]
        client.create.side_effect = [
            APIConnectionError(request=Request("POST", "https://example.invalid/v1")),
            client.stream,
        ]
        with patch.object(model_client, "load_dotenv"), \
                patch.object(model_client, "build_system_prompt", return_value="system rules"), \
                patch.object(model_client, "AsyncOpenAI", return_value=client), \
                patch.object(model_client, "measure_context_chars", return_value=4321) as measure, \
                patch("mini_claude.retry.asyncio.sleep", new_callable=AsyncMock), \
                patch("mini_claude.retry.sys.stderr", new=io.StringIO()), \
                patch.dict(os.environ, {
                    "OPENAI_API_KEY": "test-only",
                    "OPENAI_API_BASE": "https://example.invalid/v1",
                    "OPENAI_MODEL": "test",
                }):
            result = asyncio.run(model_client.ask_model(history, on_context=on_context))

        self.assertEqual(result.content, "Hello world")
        self.assertEqual(client.create.await_count, 2)
        measure.assert_called_once_with(
            [{"role": "system", "content": "system rules"}, *history],
            model_client.TOOL_DEFINITIONS,
        )
        on_context.assert_called_once_with(4321)
        for request in client.create.call_args_list:
            self.assertEqual(request.kwargs["messages"], measure.call_args.args[0])
            self.assertEqual(request.kwargs["tools"], measure.call_args.args[1])
        self.assertEqual(history, [{"role": "user", "content": "hello"}])

    def test_current_preflight_replaces_old_context_when_request_fails_before_callback(self):
        for streaming in (False, True):
            with self.subTest(streaming=streaming):
                agent = Agent()
                calls = 0

                async def model(messages, *, on_text=None, on_usage=None, on_context=None):
                    nonlocal calls
                    calls += 1
                    if calls == 2:
                        raise ValueError("configuration failed before measurement")
                    on_context(4321)
                    return ChatCompletionMessage(role="assistant", content="Done")

                with patch("mini_claude.agent.ask_model", new=model), \
                        patch("mini_claude.agent.build_system_prompt", return_value="rules"):
                    on_text = Mock() if streaming else None
                    asyncio.run(agent.chat("first", on_text=on_text))
                    self.assertEqual(agent.context_chars, 4321)
                    with self.assertRaisesRegex(ValueError, "configuration failed"):
                        asyncio.run(agent.chat("second", on_text=on_text))
                expected = measure_context_chars([
                    {"role": "system", "content": "rules"},
                    *agent.messages,
                    {"role": "user", "content": "second"},
                ], model_client.TOOL_DEFINITIONS)
                self.assertEqual(agent.context_chars, expected)
                self.assertEqual(agent.turn_usage.missing_requests, 1)
                self.assertEqual(len(agent.messages), 2)

    def test_context_resets_if_next_preflight_fails_before_measurement(self):
        agent = Agent()

        async def model(messages, *, on_text=None, on_usage=None, on_context=None):
            on_context(4321)
            return ChatCompletionMessage(role="assistant", content="Done")

        with patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=model)) as request, \
                patch("mini_claude.agent.build_system_prompt", side_effect=[
                    "rules", ValueError("project rules unavailable"),
                ]):
            asyncio.run(agent.chat("first"))
            self.assertEqual(agent.context_chars, 4321)
            with self.assertRaisesRegex(ValueError, "project rules unavailable"):
                asyncio.run(agent.chat("second"))
        self.assertIsNone(agent.context_chars)
        self.assertEqual(request.await_count, 1)
        self.assertEqual(agent.turn_usage.missing_requests, 0)
        self.assertEqual(len(agent.messages), 2)

    def test_unified_summary_entry_uses_summary_prompt_and_no_tools(self):
        history = [{"role": "user", "content": "Remember my constraints"}]
        for finish_reason in ("stop", "length"):
            with self.subTest(finish_reason=finish_reason):
                client = FakeClient()
                completion = ChatCompletion(
                    id="summary_test", created=0, model="test", object="chat.completion",
                    choices=[{
                        "index": 0, "finish_reason": finish_reason,
                        "message": {"role": "assistant", "content": "Summary"},
                    }],
                    usage={"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
                )
                client.create.return_value = completion
                on_usage, on_text, on_context = Mock(), Mock(), Mock()
                with patch.object(model_client, "load_dotenv"), \
                        patch.object(model_client, "build_system_prompt") as project_prompt, \
                        patch.object(model_client, "AsyncOpenAI", return_value=client), \
                        patch.dict(os.environ, {
                            "OPENAI_API_KEY": "test-only",
                            "OPENAI_API_BASE": "https://example.invalid/v1",
                            "OPENAI_MODEL": "test",
                        }):
                    async def summarize():
                        return await model_client.ask_model(
                            history, purpose="summary", on_usage=on_usage,
                            on_text=on_text, on_context=on_context,
                        )

                    if finish_reason == "stop":
                        answer = asyncio.run(summarize())
                        self.assertIsInstance(answer, ChatCompletionMessage)
                        self.assertEqual(answer.content, "Summary")
                        self.assertFalse(answer.tool_calls)
                    else:
                        with self.assertRaises(ValueError):
                            asyncio.run(summarize())
                client.create.assert_awaited_once()
                kwargs = client.create.call_args.kwargs
                self.assertFalse(kwargs["stream"])
                self.assertNotIn("tools", kwargs)
                self.assertEqual(kwargs["messages"][0]["content"], SUMMARY_PROMPT)
                self.assertEqual(
                    json.loads(kwargs["messages"][1]["content"]),
                    {"conversation": history},
                )
                project_prompt.assert_not_called()
                on_usage.assert_called_once_with(completion.usage)
                on_text.assert_not_called()
                on_context.assert_not_called()
                self.assertTrue(client.closed)

    def test_unknown_purpose_is_rejected_before_loading_configuration(self):
        for purpose in ("purpose", "unknown"):
            with self.subTest(purpose=purpose), \
                    patch.object(model_client, "load_dotenv") as load, \
                    patch.object(model_client, "AsyncOpenAI") as factory:
                with self.assertRaises(ValueError):
                    asyncio.run(model_client.ask_model([], purpose=purpose))
                load.assert_not_called()
                factory.assert_not_called()

    def test_interleaved_arguments_are_assembled_by_index(self):
        chunks = [
            chunk(tool_calls=[{
                "index": 1, "id": "call_b", "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path":'},
            }]),
            chunk(tool_calls=[{
                "index": 0, "id": "call_a", "type": "function",
                "function": {"name": "read_file", "arguments": '{"file_path":'},
            }]),
            chunk(tool_calls=[{
                "index": 0, "function": {"arguments": '"README.md"}'},
            }]),
            chunk(tool_calls=[{
                "index": 1, "function": {"arguments": '"mini_claude/agent.py"}'},
            }]),
            chunk(finish_reason="tool_calls"),
        ]
        stream = FakeStream(chunks)
        result = asyncio.run(model_client.collect_stream(stream))
        self.assertEqual([call.id for call in result.tool_calls], ["call_a", "call_b"])
        self.assertEqual([call.function.name for call in result.tool_calls], ["read_file"] * 2)
        self.assertEqual(
            [json.loads(call.function.arguments) for call in result.tool_calls],
            [{"file_path": "README.md"}, {"file_path": "mini_claude/agent.py"}],
        )
        self.assertTrue(stream.closed)

    def test_empty_choice_chunk_is_skipped(self):
        empty = ChatCompletionChunk(
            id="completion_test", created=0, model="test",
            object="chat.completion.chunk", choices=[],
        )
        result = asyncio.run(model_client.collect_stream(FakeStream([
            empty, chunk(content="ok"), chunk(finish_reason="stop"),
        ])))
        self.assertEqual(result.content, "ok")
        self.assertIsNone(result.tool_calls)

    def test_unfinished_tool_stream_does_not_execute_or_commit_history(self):
        for finish_reason in (None, "length"):
            with self.subTest(finish_reason=finish_reason):
                chunks = [chunk(tool_calls=[{
                    "index": 0, "id": "call_write", "type": "function",
                    "function": {"name": "write_file", "arguments": '{"file_path":'},
                }])]
                if finish_reason is not None:
                    chunks.append(chunk(finish_reason=finish_reason))
                stream = FakeStream(chunks)

                async def model(messages, *, on_usage=None, on_context=None):
                    return await model_client.collect_stream(stream, on_usage=on_usage)

                agent = Agent()
                previous = [{"role": "user", "content": "previous"}]
                agent.messages = previous.copy()
                with patch("mini_claude.agent.ask_model", new=model), \
                        patch("mini_claude.agent.execute_tool") as execute:
                    with self.assertRaises(ValueError):
                        asyncio.run(agent.chat("write a file"))
                    execute.assert_not_called()
                self.assertEqual(agent.messages, previous)
                self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
