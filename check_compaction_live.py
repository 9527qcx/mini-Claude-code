"""使用真实模型验收自动压缩；只发送模拟资料，预计三次模型请求。"""

import asyncio
import copy
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from uuid import uuid4

from httpx import RequestError
from openai import APIError

import mini_claude.agent as agent_module
from mini_claude.__main__ import show_usage
from mini_claude.context import measure_context_chars
from mini_claude.session import load_session
from mini_claude.tools import TOOL_DEFINITIONS


TEST_RULES = (
    "这是自动压缩验收。仅依据输入的模拟资料回答，不调用任何工具。"
    "区分已经完成和尚未验证的事项，保留精确标识。"
    "用户要求 JSON 时，只输出 JSON 对象，不使用 Markdown 围栏。"
)


def build_fixture(marker):
    expected = {
        "editable_file": "client.py",
        "protected_file": "agent.py",
        "verified_dependency": "dotenv",
        "pending_dependency": "openai",
        "validation_marker": marker,
        "python_reinstalled": False,
    }
    history = [
        {
            "role": "user",
            "content": (
                "以下是任务的约束和已核实状态，请准确保留：\n"
                + json.dumps(expected, ensure_ascii=False)
            ),
        },
        {
            "role": "assistant",
            "content": (
                "收到，只允许修改指定文件，禁止重装 Python。\n"
                + "Repeated checkpoint: no additional changes or verification.\n" * 300
                + "以上重复记录没有新增事实；openai 仍未验证。"
            ),
        },
    ]
    return history, expected


async def main():
    workspace = Path(__file__).resolve().parent
    history, expected = build_fixture("probe-" + uuid4().hex[:12])
    original = copy.deepcopy(history)
    system = {"role": "system", "content": TEST_RULES}
    limit = measure_context_chars([system], TOOL_DEFINITIONS) + 4000
    question = (
        "根据之前记录，仅输出一个 JSON 对象，包含以下六个键及对应的准确值："
        "editable_file、protected_file、verified_dependency、pending_dependency、"
        "validation_marker、python_reinstalled。不要调用工具。"
    )
    before = measure_context_chars(
        [system, *history, {"role": "user", "content": question}],
        TOOL_DEFINITIONS,
    )
    assert before > limit, "模拟资料没有达到触发阈值"
    calls = []
    request_model = agent_module.ask_model

    async def counted_request(messages, *, purpose="chat", **kwargs):
        calls.append(purpose)
        return await request_model(messages, purpose=purpose, **kwargs)

    with TemporaryDirectory(prefix=".compaction-live-", dir=workspace) as directory:
        root = Path(directory).resolve()
        if not root.is_relative_to(workspace.resolve()):
            raise AssertionError("临时目录不在工作区内")
        agent = agent_module.Agent(
            max_turns=1, permission_mode="read_only", session_path=root / "session.json",
        )
        agent.messages = copy.deepcopy(history)

        # 只调整本次验收；生产阈值和项目规则文件都不修改。
        with patch("mini_claude.agent.CONTEXT_TRIM_TRIGGER_CHARS", limit), \
                patch("mini_claude.agent.build_system_prompt", return_value=TEST_RULES), \
                patch("mini_claude.client.build_system_prompt", return_value=TEST_RULES), \
                patch.object(agent_module, "ask_model", new=counted_request), \
                patch.object(agent_module, "execute_tool", side_effect=AssertionError("验收不执行工具")):
            print(f"模拟上下文：{before} 字符；本次触发阈值：{limit} 字符")
            try:
                answer = await agent.chat(question)
                print("第一轮回答：", answer)
            finally:
                show_usage(agent.turn_usage)

            assert json.loads(answer) == expected, "压缩后未准确保留约束、状态或验收标记"
            assert calls == ["summary", "chat"], "第一轮请求顺序不符合预期"
            assert agent.summary_covered_count == len(history) + 1, "摘要覆盖位置不正确"
            assert agent.messages[:len(history)] == original, "原始长历史发生变化"
            assert load_session(agent.session_path) == agent.messages, "完整历史保存不一致"
            assert agent.turn_usage.reported_requests + agent.turn_usage.missing_requests == 2
            covered = agent.summary_covered_count
            assert agent.context_chars <= limit, "压缩后上下文仍超过阈值"

            try:
                answer = await agent.chat(
                    "保持上一答的六个键和值，再次只输出同一个 JSON 对象，不要调用工具。"
                )
                print("第二轮回答：", answer)
            finally:
                show_usage(agent.turn_usage)

            assert json.loads(answer) == expected, "下一轮没有保持已核实的事实"
            assert calls == ["summary", "chat", "chat"], "下一轮没有复用摘要缓存"
            assert agent.summary_covered_count == covered, "缓存覆盖位置意外变化"
            assert agent.turn_usage.reported_requests + agent.turn_usage.missing_requests == 1
            assert load_session(agent.session_path) == agent.messages, "后续历史保存不一致"

    print("验收通过：摘要后事实正确、完整历史保留、下一轮复用缓存。")
    print("临时会话已清理；项目文件未被模型工具修改。")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (APIError, RequestError, ValueError, AssertionError, OSError) as error:
        print(f"验收失败：{type(error).__name__}", file=sys.stderr)
        if isinstance(error, AssertionError):
            print(str(error), file=sys.stderr)
        raise SystemExit(1)
