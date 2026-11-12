from .client import ask_model
from .permissions import check_permission
import json
import sys
from openai import APIError
from httpx import RequestError
from pathlib import Path
from collections.abc import Callable
import asyncio
from .usage import UsageStats
from .tools import execute_tool, TOOL_DEFINITIONS
from .session import save_session, load_session, validate_tool_pairs
from .prompt import build_system_prompt
from .context import (
    measure_context_chars,
    build_summary_messages,
    CONTEXT_TRIM_TRIGGER_CHARS,
)
from .recall import (
    select_relevant_memories,
    build_recalled_memory_message,
)
from .memory import list_memories

class Agent:
    def __init__(self, max_turns: int = 10,
                 permission_mode: str ="default",
                 session_path: Path | None = None,
                 enable_memory_recall: bool = False,
                 ):
        if max_turns < 1:
            raise ValueError("max_turns 必须大于 0")
        self.messages = []
        self.max_turns = max_turns
        self.permission_mode = permission_mode
        self.read_state: dict[str, int] = {} #对应文件的最后的修改时间
        self.session_path = session_path
        self.turn_usage = UsageStats()
        self.context_chars:int | None = None #上下文长度
        self.summary_messages: list = []
        self.summary_covered_count = 0
        self.enable_memory_recall = enable_memory_recall
        self.memory_directory = (
            Path.cwd() / ".mini-claude" / "memory"
        ).resolve()

        try:
            self.memory_candidates = list_memories(
                self.memory_directory
            )
        except (OSError, ValueError) as error:
            print(
                f"警告：无法加载启动记忆索引（{type(error).__name__}），"
                "本次会话使用空索引。",
                file=sys.stderr,
            )
            self.memory_candidates = []

    async def chat(
        self, msg: str, *, on_text: Callable[[str], None] | None = None
    ) -> str:
        self.turn_usage = UsageStats()
        self.context_chars = None
        memory_message = None

        if self.enable_memory_recall:
            memory_message = await self._recall_memory(msg)

        request_messages = self.messages.copy()

        if memory_message is not None:
            request_messages.append(memory_message)

        user_message = {
            "role": "user",
            "content": msg,
        }
        request_messages.append(user_message)
        
        def record_context(chars: int) -> None:
            self.context_chars = chars


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

            await self._maybe_compact_context(
                request_messages,
                current_request=msg,
            )
            model_messages = self._build_context_messages(request_messages)

            try:
                if on_text is None:
                    response = await ask_model(
                        model_messages,
                        on_usage=record_usage,
                        on_context=record_context,
                        memory_candidates=self.memory_candidates,
                    )
                else:
                    response = await ask_model(
                        model_messages,
                        on_text=emit,
                        on_usage=record_usage,
                        on_context=record_context,
                        memory_candidates=self.memory_candidates,
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

    def _build_context_messages(self, history: list) -> list:
        covered = self.summary_covered_count

        if not 0 <= covered <= len(history):
            raise ValueError("摘要覆盖位置与完整历史不一致")

        return self.summary_messages + history[covered:]

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
        self.summary_messages = []
        self.summary_covered_count = 0

    def clear_history(self):
        self.messages.clear()
        self.read_state.clear()
        self._auto_save()
        self.summary_messages = []
        self.summary_covered_count = 0

    async def _maybe_compact_context(
            self,
            history: list,
            current_request: str,
        ) -> None:
        snapshot = history.copy()

        model_messages = self._build_context_messages(snapshot)
        system_message = {
            "role": "system",
            "content": build_system_prompt(memory_candidates=self.memory_candidates),
        }

        before_chars = measure_context_chars(
            [system_message, *model_messages],
            TOOL_DEFINITIONS,
        )
        self.context_chars = before_chars

        if before_chars <= CONTEXT_TRIM_TRIGGER_CHARS:
            return

        validate_tool_pairs(snapshot)
        print("[上下文] 正在生成摘要……")

        response = await ask_model(
            model_messages,
            purpose="summary",
            on_usage=self.turn_usage.record,
        )

        candidate = build_summary_messages(
            response.content or "",
            current_request,
        )

        after_chars = measure_context_chars(
            [system_message, *candidate],
            TOOL_DEFINITIONS,
        )

        if after_chars >= before_chars:
            raise ValueError("摘要没有缩短上下文，保留原始历史")

        if after_chars > CONTEXT_TRIM_TRIGGER_CHARS:
            raise ValueError(
                "摘要后的上下文仍超过设定阈值，"
                "请调整阈值或缩小本次输入"
            )

        # 所有检查通过后，再提交状态。
        # 先保留完整历史，保证覆盖位置有对应的原始消息。
        self.messages = snapshot
        self._auto_save()

        self.summary_messages = candidate
        self.summary_covered_count = len(snapshot)
        self.context_chars = after_chars

        print(
            f"[上下文] 已压缩："
            f"{before_chars} → {after_chars} 字符"
        )

    async def _recall_memory(
        self,
        query: str,
    ) -> dict[str, str] | None:
        try:
            memories = await asyncio.wait_for(
                select_relevant_memories(
                    query,
                    self.memory_directory,
                    candidates=self.memory_candidates,
                    on_usage=self.turn_usage.record,
                ),
                timeout=15,
            )

            return build_recalled_memory_message(memories)

        except (
            TimeoutError,
            APIError,
            RequestError,
            OSError,
            ValueError,
        ) as error:
            print(
                f"警告：记忆召回失败（{type(error).__name__}），"
                "继续处理当前请求。",
                file=sys.stderr,
            )
            return None 

