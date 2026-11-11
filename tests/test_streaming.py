import asyncio
import io
import json
import os
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from openai.types.chat import ChatCompletionChunk, ChatCompletionMessage

import mini_claude.__main__ as cli
import mini_claude.client as model_client
from mini_claude.agent import Agent


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
                    self.assertEqual(output.getvalue(), "助手：Hello\n")
                    self.assertEqual(agent.messages, [])
                else:
                    self.assertEqual(output.getvalue(), "助手：Hello world\n")
                    self.assertEqual(agent.messages[-1]["content"], "Hello world")
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

                async def model(messages, *, on_text):
                    response = next(responses)
                    on_text(response.content)
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
                self.assertEqual(output.getvalue(), prefix + ending)
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

                async def model(messages):
                    return await model_client.collect_stream(stream)

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
