import asyncio
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

from openai.types.chat import ChatCompletionMessage

from mini_claude.agent import Agent
from mini_claude.memory import list_memories, read_memory
from mini_claude.prompt import build_system_prompt
from mini_claude.session import validate_tool_pairs
from mini_claude.tools import TOOL_DEFINITIONS, execute_tool


def tool_request(name, arguments):
    return ChatCompletionMessage(
        role="assistant", content=None,
        tool_calls=[{
            "id": "memory_call", "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }],
    )


class MemoryToolTests(unittest.TestCase):
    def setUp(self):
        workspace = Path.cwd().resolve()
        temporary = TemporaryDirectory(prefix=".memory-tool-check-", dir=workspace)
        self.root = Path(temporary.name).resolve()
        if not self.root.is_relative_to(workspace):
            raise AssertionError("Temporary directory escaped the workspace")
        self.addCleanup(temporary.cleanup)
        self.enterContext(patch.object(Path, "cwd", return_value=self.root))
        self.enterContext(patch("mini_claude.prompt.get_git_context", return_value=""))
        self.enterContext(redirect_stdout(io.StringIO()))
        self.arguments = {"title": "Python 学习偏好", "content": "中文逐步教学，先讲思路再让用户写代码。"}
        self.directory = self.root / ".mini-claude" / "memory"

    def test_schema_exposes_content_without_model_controlled_directory(self):
        definitions = [
            item["function"] for item in TOOL_DEFINITIONS
            if item["function"]["name"] == "save_memory"
        ]
        self.assertEqual(len(definitions), 1)
        parameters = definitions[0]["parameters"]
        self.assertEqual(set(parameters["required"]), {"title", "content"})
        self.assertEqual(set(parameters["properties"]), {"title", "content"})
        self.assertFalse(parameters["additionalProperties"])

    def test_dispatch_saves_in_project_memory_directory_without_file_path(self):
        read_state = {"unrelated-file": 123}
        result = execute_tool("save_memory", self.arguments, read_state=read_state)
        self.assertFalse(result["is_error"])
        items = list_memories(self.directory)
        self.assertEqual(len(items), 1)
        memory = read_memory(self.directory, items[0]["id"])
        self.assertEqual(memory["title"], self.arguments["title"])
        self.assertEqual(memory["content"], self.arguments["content"])
        self.assertIn(memory["id"], result["content"])
        self.assertEqual(read_state, {"unrelated-file": 123})

    def test_invalid_arguments_and_storage_failures_return_tool_errors(self):
        for arguments in (
            None, [], "text", {}, {"title": "title"}, {"content": "body"},
            {"title": " ", "content": "body"}, {"title": "title", "content": 42},
        ):
            with self.subTest(arguments=arguments):
                result = execute_tool("save_memory", arguments)
                self.assertTrue(result["is_error"])
                self.assertFalse(self.directory.exists())
        with patch("mini_claude.tools.save_memory", side_effect=OSError("storage unavailable")):
            result = execute_tool("save_memory", self.arguments)
        self.assertTrue(result["is_error"])
        self.assertIn("storage unavailable", result["content"])
        self.assertFalse(self.directory.exists())

    def test_agent_confirmation_and_read_only_mode_control_persistence(self):
        cases = [
            ("approved", "default", "y", True),
            ("rejected", "default", "n", False),
            ("read-only", "read_only", "y", False),
        ]
        for label, permission_mode, answer, allowed in cases:
            with self.subTest(case=label):
                project = self.root / label
                project.mkdir()
                agent = Agent(permission_mode=permission_mode)
                with patch.object(Path, "cwd", return_value=project), \
                        patch("mini_claude.agent.ask_model", new_callable=AsyncMock) as model, \
                        patch("mini_claude.agent.execute_tool", wraps=execute_tool) as execute, \
                        patch("builtins.input", return_value=answer) as confirm:
                    model.side_effect = [
                        tool_request("save_memory", self.arguments),
                        ChatCompletionMessage(role="assistant", content="Handled"),
                    ]
                    self.assertEqual(asyncio.run(agent.chat("记住我的学习偏好")), "Handled")
                self.assertEqual(model.await_count, 2)
                self.assertEqual(execute.call_count, 1 if allowed else 0)
                if permission_mode == "read_only":
                    confirm.assert_not_called()
                else:
                    confirm.assert_called_once()
                result = json.loads(agent.messages[-2]["content"])
                self.assertEqual(result["is_error"], not allowed)
                validate_tool_pairs(agent.messages)
                directory = project / ".mini-claude" / "memory"
                self.assertEqual(len(list_memories(directory)), 1 if allowed else 0)
                if not allowed:
                    self.assertFalse(directory.exists())

    def test_fresh_agent_reads_saved_memory_from_index_without_old_session(self):
        saved = execute_tool("save_memory", self.arguments)
        self.assertFalse(saved["is_error"])
        memory_id = list_memories(self.directory)[0]["id"]
        path = self.directory / f"{memory_id}.json"
        agent = Agent()
        self.assertEqual(agent.messages, [])

        async def model(messages, **kwargs):
            if len(messages) == 1:
                index = build_system_prompt(self.root)
                self.assertIn(memory_id, index)
                self.assertIn(self.arguments["title"], index)
                self.assertNotIn(self.arguments["content"], index)
                return tool_request("read_file", {"file_path": str(path)})
            result = json.loads(messages[-1]["content"])
            self.assertFalse(result["is_error"])
            memory = json.loads(result["content"])
            return ChatCompletionMessage(role="assistant", content=memory["content"])

        with patch("mini_claude.agent.ask_model", new=AsyncMock(side_effect=model)) as request, \
                patch("builtins.input") as confirm:
            answer = asyncio.run(agent.chat("按我的 Python 学习偏好讲解"))
        self.assertEqual(answer, self.arguments["content"])
        self.assertEqual(request.await_count, 2)
        confirm.assert_not_called()
        self.assertEqual(len(agent.messages), 4)
        validate_tool_pairs(agent.messages)


if __name__ == "__main__":
    unittest.main()
