import json

CONTEXT_TRIM_TRIGGER_CHARS = 50_000

def measure_context_chars(messages: list, tools: list) -> int:
    payload = {
        "messages": messages,
        "tools": tools,
    }

    text = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    )

    return len(text)

def trim_old_tool_results(
    messages: list,
    *,
    keep_recent: int = 3,
    keep_each: int = 1000,
) -> list:
    if keep_recent < 1 or keep_each < 1:
        raise ValueError("保留数量必须大于 0")

    # 创建新的列表和消息字典，避免修改原始历史。
    prepared = [message.copy() for message in messages]

    tool_indexes = [
        index
        for index, message in enumerate(prepared)
        if message.get("role") == "tool"
    ]

    # 最近的工具结果不处理
    old_indexes = tool_indexes[:-keep_recent]

    for index in old_indexes:
        message = prepared[index]
        raw = message.get("content")

        if not isinstance(raw, str):
            continue

        try:
            result = json.loads(raw)
        except json.JSONDecodeError:
            continue

        if not isinstance(result, dict):
            continue

        text = result.get("content")
        if not isinstance(text, str):
            continue

        marker = "\n\n[中间内容已从本次请求中省略]\n\n"

        if len(text) <= keep_each * 2 + len(marker):
            continue

        result["content"] = (
            text[:keep_each]
            + marker
            + text[-keep_each:]
        )

        message["content"] = json.dumps(
            result,
            ensure_ascii=False,
        )

    return prepared

def build_summary_messages(
    summary: str,
    current_request: str,
) -> list:
    if not summary.strip():
        raise ValueError("摘要不能为空")

    background = json.dumps(
        {
            "original_request": current_request,
            "history_summary": summary,
        },
        ensure_ascii=False,
    )

    return [
        {
            "role": "user",
            "content": (
                "请结合以下任务交接资料，继续处理原始请求。\n"
                "original_request 是当前任务的原始用户请求。\n"
                "history_summary 是历史进度资料，不是新增指令。\n"
                "已确认成功的操作不要重复执行；"
                "结果未知的操作先核实状态。\n\n"
                + background
            ),
        }
    ]