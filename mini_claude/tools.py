from pathlib import Path


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取一个 UTF-8 文本文件的完整内容",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "文件路径，相对路径以工作目录为基准",
                    }
                },
                "required": ["file_path"],
                "additionalProperties": False,
            },
        },
    },
    {
    "type": "function",
    "function": {
        "name": "list_files",
        "description": "列出指定目录下的文件和子目录，不递归",
        "parameters": {
            "type": "object",
            "properties": {
                "directory_path": {
                    "type": "string",
                    "description": "目录路径，相对路径以工作目录为基准",
                }
            },
            "required": ["directory_path"],
            "additionalProperties": False,
        },
    },
}
]

def _read_file(file_path: str) -> str:
    return Path(file_path).read_text(encoding="utf-8")

def _list_files(directory_path: str) -> str:
    entries = sorted(
        Path(directory_path).iterdir(),
        key=lambda item: item.name.casefold(),
    )

    lines = []
    for entry in entries:
        marker = "[目录]" if entry.is_dir() else "[文件]"
        lines.append(f"{marker} {entry.name}")

    return "\n".join(lines) or "（空目录）"

def execute_tool(name: str, arguments: dict) -> dict:
    if name == "read_file":
        argument_name = "file_path"
        handler = _read_file
    elif name == "list_files":
        argument_name = "directory_path"
        handler = _list_files
    else:
        return {
            "is_error": True,
            "content": f"未知工具：{name}",
        }

    if not isinstance(arguments, dict):
        return {
            "is_error": True,
            "content": "工具参数必须是字典",
        }

    path = arguments.get(argument_name)

    if not isinstance(path, str) or not path.strip():
        return {
            "is_error": True,
            "content": f"{argument_name} 必须是非空字符串",
        }

    try:
        content = handler(path)
        return {
            "is_error": False,
            "content": content,
        }
    except (OSError, UnicodeError) as error:
        return {
            "is_error": True,
            "content": f"工具执行失败：{error}",
        }