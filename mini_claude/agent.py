from .client import ask_model
from .tools import execute_tool
from .permissions import check_permission
import json

class Agent:
    def __init__(self, max_turns: int = 10, permission_mode: str ="default"):
        if max_turns < 1:
            raise ValueError("max_turns 必须大于 0")
        self.messages = []
        self.max_turns = max_turns
        self.permission_mode = permission_mode

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
                    permission = check_permission(name, self.permission_mode)
                    action = permission.get("action")
                    if action=="allow":
                        result = execute_tool(name, arguments)
                    elif action=="confirm":
                        print(permission["message"])
                        print(f"工具：{name}")
                        print(json.dumps(arguments, ensure_ascii=False, indent=2))
                        try:
                            confirmed = input("允许执行？[Y/N] ").strip().lower() == "y"
                        except (EOFError, KeyboardInterrupt):
                            print()
                            confirmed = False
                        if confirmed:
                            result = execute_tool(name, arguments)
                        else:
                            result = {
                                "is_error": True,
                                "content": "用户拒绝了此次操作",
                            }
                    else:
                        result = {
                            "is_error": True,
                            "content": permission.get("message", "权限检查未通过"),
                        }

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