from .client import ask_model
from .tools import execute_tool
from .permissions import check_permission
import json
import sys
from pathlib import Path
from collections.abc import Callable
import asyncio
from .session import save_session, load_session
from .usage import UsageStats

class Agent:
    def __init__(self, max_turns: int = 10,
                 permission_mode: str ="default",
                 session_path: Path | None = None
                 ):
        if max_turns < 1:
            raise ValueError("max_turns 必须大于 0")
        self.messages = []
        self.max_turns = max_turns
        self.permission_mode = permission_mode
        self.read_state: dict[str, int] = {} #对应文件的最后的修改时间
        self.session_path = session_path
        self.turn_usage = UsageStats()

    async def chat(
        self, msg: str, *, on_text: Callable[[str], None] | None = None
    ) -> str:
        self.turn_usage = UsageStats()
        user_message = {"role": "user", "content": msg}
        request_messages = self.messages + [user_message]

        for _ in range(self.max_turns):
            usage_received = False
            last_part = ""

            def record_usage(usage):
                nonlocal usage_received

                if usage_received:
                    return

                usage_received = True
                self.turn_usage.record(usage)

            def emit(text: str) -> None:
                nonlocal last_part
                last_part = text
                if on_text is not None:
                    on_text(text)

            try:
                if on_text is None:
                    response = await ask_model(
                        request_messages,
                        on_usage=record_usage,
                    )
                else:
                    response = await ask_model(
                        request_messages,
                        on_text=emit,
                        on_usage=record_usage,
                    )
            finally:
                if not usage_received:
                    self.turn_usage.record(None)

                if (on_text is not None and last_part and not last_part.endswith("\n")):
                    on_text("\n")

            request_messages.append(
                response.model_dump(exclude_none=True)
            )

            if not response.tool_calls:
                self.messages = request_messages
                self._auto_save()
                return response.content or ""

            completed_count = 0
            try:
                for tool_call in response.tool_calls:
                    await asyncio.sleep(0)

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
                            result = execute_tool(name, arguments, read_state=self.read_state)
                        elif action=="confirm":
                            print(permission["message"])
                            print(f"工具：{name}")
                            print(json.dumps(arguments, ensure_ascii=False, indent=2))
                            try:
                                confirmed = input("允许执行？[Y/N] ").strip().lower() == "y"
                            except EOFError:
                                print()
                                confirmed = False

                            # 执行工具前，处理已经收到的取消请求。
                            await asyncio.sleep(0)

                            if confirmed:
                                result = execute_tool(name, arguments, read_state=self.read_state)
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

                    completed_count += 1
            except (asyncio.CancelledError, KeyboardInterrupt):
                for pending_call in response.tool_calls[completed_count:]:
                    interrupted_result = {
                        "is_error": True,
                        "content": (
                            "本轮已中断，此工具调用未取得完整结果。"
                            "这不代表操作没有发生；"
                            "再次执行前请检查实际状态。"
                        ),
                    }

                    request_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": pending_call.id,
                            "content": json.dumps(
                                interrupted_result,
                                ensure_ascii=False,
                            ),
                        }
                    )

                self.messages = request_messages.copy()
                self._auto_save()
                raise

            self.messages = request_messages.copy()
            self._auto_save()

        notice = f"已达到 {self.max_turns} 次模型调用上限，本轮停止。"
        request_messages.append(
            {"role": "assistant", "content": notice}
        )
        self.messages = request_messages
        self._auto_save()

        if on_text is not None:
            on_text(notice + "\n")
        return notice

    def _auto_save(self):
        if self.session_path is None:
            return

        try:
            save_session(self.session_path, self.messages)
        except (OSError, TypeError, ValueError) as error:
            print(f"警告：会话保存失败：{error}", file=sys.stderr)

    def restore_session(self, path: Path) -> None:
        messages = load_session(path)

        self.messages = messages
        self.session_path = Path(path)
        self.read_state.clear()

    def clear_history(self):
        self.messages.clear()
        self.read_state.clear()
        self._auto_save()
        
