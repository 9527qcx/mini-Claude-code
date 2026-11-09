from .client import ask_model
from .tools import execute_tool
import json

class Agent:
    def __init__(self, max_turns: int = 10):
        if max_turns < 1:
            raise ValueError("max_turns 必须大于 0")
        self.messages = []
        self.max_turns = max_turns

    async def chat(self, msg: str) -> str:
        user_message = {"role": "user", "content": msg}
        request_messages = self.messages + [user_message]

        for _ in range(self.max_turns):
            response = await ask_model(request_messages)

            request_messages.append(
                response.model_dump(exclude_none=True)
            )

            if not response.tool_calls:
                self.messages = request_messages
                return response.content or ""

            for tool_call in response.tool_calls:
                name = tool_call.function.name
                print(f"[工具调用] {name}")

                try:
                    arguments = json.loads(
                        tool_call.function.arguments
                    )
                except json.JSONDecodeError:
                    result = {
                        "is_error": True,
                        "content": "工具参数不是合法 JSON，请修正参数",
                    }
                else:
                    result = execute_tool(name, arguments)

                status = "失败" if result["is_error"] else "成功"
                print(f"[工具结果] {status}")

                request_messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": json.dumps(
                            result,
                            ensure_ascii=False,
                        ),
                    }
                )

        notice = f"已达到 {self.max_turns} 次模型调用上限，本轮停止。"
        request_messages.append(
            {"role": "assistant", "content": notice}
        )
        self.messages = request_messages
        return notice

        

    def clear_history(self):
        self.messages.clear()