import json
from pathlib import Path


def save_session(path: Path, messages: list) -> None:
    path = Path(path)

    data = {
        "version": 1,
        "messages": messages,
    }

    text = json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    )

    path.parent.mkdir(parents=True, exist_ok=True)

    #先写入临时文件，再覆盖原文件
    temporary_path = path.with_name(path.name + ".tmp")
    temporary_path.write_text(text, encoding="utf-8")
    temporary_path.replace(path)


def load_session(path: Path) -> list:
    path = Path(path)

    try:
        text = path.read_text(encoding="utf-8-sig")
        data = json.loads(text)

    except FileNotFoundError as error:
        raise ValueError(f"未找到会话文件：{path}") from error

    except json.JSONDecodeError as error:
        raise ValueError(f"会话文件不是合法 JSON：{path}") from error

    except (OSError, UnicodeError) as error:
        raise ValueError(f"读取会话失败：{error}") from error

    if not isinstance(data, dict) or data.get("version") != 1:
        raise ValueError("会话文件格式或版本不受支持")

    messages = data.get("messages")

    if not isinstance(messages, list):
        raise ValueError("会话中的 messages 必须是列表")

    if any(not isinstance(message, dict) for message in messages):
        raise ValueError("每条历史消息必须是字典")

    validate_tool_pairs(messages)
    return messages

def find_latest_session(session_dir: Path):
    session_dir = Path(session_dir)

    try:
        files = [
            path 
            for path in session_dir.glob("*.json")
            if path.is_file()
        ]

        if not files:
            raise ValueError("没有可恢复的会话")

        return max(
            files,
            key=lambda path: (
                path.stat().st_mtime_ns,
                path.name,
            ),
        )
    except OSError as error:
        raise ValueError(f"查找最近会话失败：{error}") from error

def validate_tool_pairs(messages: list) ->None:
    pending = set()

    for index, msg in enumerate(messages):
        role = msg.get("role")

        if role == "tool":
            call_id = msg.get("tool_call_id")

            if not isinstance(call_id, str) or call_id not in pending:
                raise ValueError(
                    f"第 {index + 1} 条消息的工具结果没有对应调用"
                )

            pending.remove(call_id)
            continue

        if pending:
            raise ValueError("工具结果尚未补齐，就出现了其他消息")

        if role not in {"user", "assistant"}:
            raise ValueError(f"第 {index + 1} 条消息的角色不受支持")

        if role == "assistant":
            calls = msg.get("tool_calls", [])

            if not isinstance(calls, list):
                raise ValueError("tool_calls 必须是列表")

            for call in calls:
                if not isinstance(call, dict):
                    raise ValueError("每个工具调用必须是字典")

                call_id = call.get("id")

                if not isinstance(call_id, str) or not call_id:
                    raise ValueError("工具调用缺少有效 ID")

                if call_id in pending:
                    raise ValueError("同一批工具调用的 ID 重复")

                pending.add(call_id)

    if pending:
        raise ValueError("会话结束时仍有工具调用缺少结果")

