import copy
import json
import unittest

from mini_claude.context import measure_context_chars, trim_old_tool_results
from mini_claude.context import build_summary_messages
from mini_claude.agent import Agent
from mini_claude.session import validate_tool_pairs


class ContextTests(unittest.TestCase):
    def test_summary_view_preserves_full_history_and_appends_uncovered_messages(self):
        history = [
            {"role": "user", "content": "Read the project"},
            {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "read succeeded"},
            {"role": "assistant", "content": "Read completed"},
            {"role": "user", "content": "Now explain the result"},
        ]
        original = copy.deepcopy(history)
        agent = Agent()
        agent.summary_messages = build_summary_messages(
            "The file was read successfully.", "Read the project",
        )
        agent.summary_covered_count = 3
        cached_summary = copy.deepcopy(agent.summary_messages)

        prepared = agent._build_context_messages(history)
        self.assertEqual(prepared, cached_summary + history[3:])
        validate_tool_pairs(prepared)
        self.assertEqual(history, original)

        history.append({"role": "assistant", "content": "Here is the explanation"})
        updated = agent._build_context_messages(history)
        self.assertEqual(updated, cached_summary + history[3:])
        self.assertEqual(agent.summary_covered_count, 3)
        self.assertEqual(agent.summary_messages, cached_summary)
        self.assertEqual(prepared, cached_summary + original[3:])

    def test_summary_view_rejects_invalid_coverage(self):
        agent = Agent()
        history = [{"role": "user", "content": "hello"}]
        for count in (-1, 2):
            with self.subTest(count=count), self.assertRaises(ValueError):
                agent.summary_covered_count = count
                agent._build_context_messages(history)

    def test_clearing_history_also_clears_summary_cache(self):
        agent = Agent()
        agent.messages = [{"role": "user", "content": "old task"}]
        agent.summary_messages = build_summary_messages("Old task progress", "old task")
        agent.summary_covered_count = 1
        agent.clear_history()
        self.assertEqual(agent.messages, [])
        self.assertEqual(agent.summary_messages, [])
        self.assertEqual(agent.summary_covered_count, 0)
        new_history = [{"role": "user", "content": "new task"}]
        self.assertEqual(agent._build_context_messages(new_history), new_history)

    def test_unicode_is_counted_as_characters_without_ascii_escaping(self):
        messages = [{"role": "user", "content": "你好"}]
        expected = '{"messages":[{"role":"user","content":"你好"}],"tools":[]}'
        self.assertEqual(measure_context_chars(messages, []), len(expected))

    def test_history_and_tool_definitions_contribute_without_being_modified(self):
        messages = [{"role": "system", "content": "rules"}]
        tools = [{
            "type": "function",
            "function": {"name": "read_file", "description": "read text"},
        }]
        original_messages = copy.deepcopy(messages)
        original_tools = copy.deepcopy(tools)
        baseline = measure_context_chars(messages, [])
        with_tools = measure_context_chars(messages, tools)
        self.assertGreater(with_tools, baseline)
        self.assertEqual(messages, original_messages)
        self.assertEqual(tools, original_tools)

        longer = messages + [{"role": "user", "content": "More context" * 100}]
        self.assertGreater(measure_context_chars(longer, tools), with_tools)
        self.assertEqual(measure_context_chars(messages, tools), with_tools)

    def test_trim_preserves_recent_results_status_and_tool_pairs(self):
        messages = [{"role": "user", "content": "Keep this task unchanged"}]
        for index in range(5):
            messages.extend([
                {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": f"call_{index}", "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }],
                },
                {
                    "role": "tool", "tool_call_id": f"call_{index}",
                    "content": json.dumps({
                        "is_error": index == 0,
                        "content": f"START-{index}" + "长内容" * 1000 + f"END-{index}",
                        "extra": {"file_path": f"file-{index}.txt"},
                    }, ensure_ascii=False),
                },
            ])
        messages.append({"role": "assistant", "content": "Keep this answer unchanged"})
        original = copy.deepcopy(messages)

        for keep_recent in (1, 3, 8):
            with self.subTest(keep_recent=keep_recent):
                prepared = trim_old_tool_results(
                    messages, keep_recent=keep_recent, keep_each=20,
                )
                self.assertEqual(messages, original)
                self.assertEqual(len(prepared), len(messages))
                validate_tool_pairs(prepared)
                self.assertLessEqual(
                    measure_context_chars(prepared, []),
                    measure_context_chars(messages, []),
                )
                tool_number = 0
                for before, after in zip(messages, prepared):
                    if before["role"] != "tool":
                        self.assertEqual(after, before)
                        continue
                    self.assertEqual(after["tool_call_id"], before["tool_call_id"])
                    if tool_number >= 5 - keep_recent:
                        self.assertEqual(after, before)
                    else:
                        old_result = json.loads(before["content"])
                        result = json.loads(after["content"])
                        self.assertEqual(result["is_error"], old_result["is_error"])
                        self.assertEqual(result["extra"], old_result["extra"])
                        self.assertTrue(result["content"].startswith(old_result["content"][:20]))
                        self.assertTrue(result["content"].endswith(old_result["content"][-20:]))
                        self.assertIn("中间内容已从本次请求中省略", result["content"])
                        self.assertLess(len(result["content"]), len(old_result["content"]))
                    tool_number += 1

    def test_trim_leaves_short_or_unstructured_results_unchanged(self):
        contents = [
            json.dumps({"is_error": False, "content": "short"}),
            "legacy result without JSON",
            json.dumps(["not a result dictionary"]),
            json.dumps({"is_error": False, "content": 42}),
            42,
            json.dumps({"is_error": False, "content": "recent" * 1000}),
        ]
        messages = [
            {"role": "tool", "tool_call_id": f"call_{index}", "content": content}
            for index, content in enumerate(contents)
        ]
        original = copy.deepcopy(messages)
        self.assertEqual(
            trim_old_tool_results(messages, keep_recent=1, keep_each=20),
            original,
        )
        self.assertEqual(messages, original)

    def test_trim_rejects_nonpositive_retention_settings(self):
        for settings in (
            {"keep_recent": 0}, {"keep_recent": -1},
            {"keep_each": 0}, {"keep_each": -1},
        ):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                trim_old_tool_results([], **settings)


if __name__ == "__main__":
    unittest.main()
