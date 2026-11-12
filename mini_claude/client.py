from pathlib import Path
from collections.abc import Callable
import os
from dotenv import load_dotenv
from openai import AsyncOpenAI
from .tools import TOOL_DEFINITIONS
from openai.types.chat import ChatCompletionMessage
from .retry import with_retry
from .prompt import build_system_prompt
from .context import (
    measure_context_chars,
    CONTEXT_TRIM_TRIGGER_CHARS,
    build_summary_messages,
)
from .summarize import summarize_context
from .memory import MEMORY_SELECTION_PROMPT





async def ask_model(
    messages, *,
    purpose: str = "chat",
    on_text: Callable[[str], None] | None = None,
    on_usage=None,
    on_context: Callable[[int], None] | None = None,
    memory_candidates: list[dict[str, str]] | None = None,
) -> ChatCompletionMessage:
    if purpose not in ("chat", "summary", "memory_select"):
        raise ValueError(f"未知请求用途：{purpose}")

    env_path = Path(__file__).resolve().parent.parent/".env"
    load_dotenv(env_path, encoding="utf-8")

    for name in ("OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_MODEL"):
        if not os.getenv(name, "").strip():
            raise ValueError(f"缺少环境变量：{name}")

    async with AsyncOpenAI(
        api_key=os.environ["OPENAI_API_KEY"],
        base_url=os.environ["OPENAI_API_BASE"],
        timeout=60.0,
        max_retries=0,
    ) as client:
        if purpose == "summary":
            summary = await summarize_context(
                messages,
                client=client,
                model=os.environ["OPENAI_MODEL"],
                on_usage=on_usage,
            )

            return ChatCompletionMessage(
                role="assistant",
                content=summary,
            )

        if purpose == "memory_select":
            selection_messages = [
                {
                    "role": "system",
                    "content": MEMORY_SELECTION_PROMPT,
                },
                *messages,
            ]

            async def create_selection():
                return await client.chat.completions.create(
                    model=os.environ["OPENAI_MODEL"],
                    messages=selection_messages,
                    stream=False,
                    max_tokens=1024,
                )

            usage = None

            try:
                response = await with_retry(create_selection)
                usage = response.usage
            finally:
                if on_usage is not None:
                    on_usage(usage)
            if not response.choices:
                raise ValueError("记忆选择响应没有候选结果")

            choice = response.choices[0]
            message = choice.message

            if choice.finish_reason != "stop":
                raise ValueError("记忆选择响应未正常完成")

            if message.tool_calls:
                raise ValueError("记忆选择请求不允许调用工具")

            if not message.content or not message.content.strip():
                raise ValueError("记忆选择结果为空")

            return message

        request_messages = [
            {
                "role": "system",
                "content": build_system_prompt(memory_candidates=memory_candidates),
            },
            *messages
        ]
        context_chars = measure_context_chars(
            request_messages,
            TOOL_DEFINITIONS,
        )

        if on_context is not None:
            on_context(context_chars)

        async def create_stream():
            return await client.chat.completions.create(
                model=os.environ["OPENAI_MODEL"],
                messages=request_messages,
                tools=TOOL_DEFINITIONS,
                stream=True,
                stream_options={"include_usage": True},
            )
        stream = await with_retry(create_stream)
        return await collect_stream(stream, on_text=on_text, on_usage=on_usage)

async def collect_stream(
    stream, *, on_text: Callable[[str], None] | None = None,
    on_usage=None,
) -> ChatCompletionMessage:
    text_parts = []
    tool_calls = {}
    finish_reason = None
    usage = None

    async with stream:
        async for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if delta.content:
                text_parts.append(delta.content)
                if on_text is not None:
                    on_text(delta.content)

            for part in delta.tool_calls or []:
                if part.index not in tool_calls:
                    tool_calls[part.index] = {
                        "id": "",
                        "type": "function",
                        "function": {
                            "name": "",
                            "arguments": "",
                        },
                    }
                
                current = tool_calls[part.index]

                if part.id:
                    current["id"] = part.id

                if part.function:
                    current["function"]["name"] += (part.function.name or "")
                    current["function"]["arguments"] += (part.function.arguments or "")

            if choice.finish_reason is not None:
                finish_reason = choice.finish_reason

    if finish_reason not in {"stop", "tool_calls"}:
        raise ValueError(
            f"模型响应未正常完成：{finish_reason}"
        )

    if bool(tool_calls) != (finish_reason == "tool_calls"):
        raise ValueError("工具调用与响应结束标记不一致")

    assembled_calls = [
        tool_calls[index]
        for index in sorted(tool_calls)
    ]

    for call in assembled_calls:
        if not call["id"] or not call["function"]["name"]:
            raise ValueError("工具调用缺少 ID 或函数名")

    if on_usage is not None:
        on_usage(usage)

    return ChatCompletionMessage(
        role="assistant",
        content="".join(text_parts) or None,
        tool_calls=assembled_calls or None,     
    )
                
