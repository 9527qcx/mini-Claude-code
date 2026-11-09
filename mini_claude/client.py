from pathlib import Path
import os
from dotenv import load_dotenv
from openai import AsyncOpenAI
from .tools import TOOL_DEFINITIONS






async def ask_model(messages) -> str:
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
        response = await client.chat.completions.create(
            model=os.environ["OPENAI_MODEL"],
            messages=messages,
            tools = TOOL_DEFINITIONS,
        )

    return response.choices[0].message