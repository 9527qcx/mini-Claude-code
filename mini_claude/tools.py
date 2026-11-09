from pathlib import Path

TOOLS_NAME = ["read_file", "list_files", "write_file"]

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
    },
    {
    "type": "function",
    "function": {
        "name": "write_file",
        "description": "创建新的 UTF-8 文本文件；文件已存在时失败",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "新文件路径，父目录必须已存在",
                },
                "content": {
                    "type": "string",
                    "description": "要写入的完整文本",
                },
            },
            "required": ["file_path", "content"],
            "additionalProperties": False,
          },
      },
    },
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

def _write_file(file_path: str, content: str) -> str:
    workspace = Path.cwd().resolve()
    target = Path(file_path).resolve()

    if not target.is_relative_to(workspace):
        raise ValueError("写入路径必须位于当前工作目录范围内")

    relative_path = target.relative_to(workspace)
    if any(part.casefold() == ".git" for part in relative_path.parts):
        raise ValueError("禁止写入 .git 目录")                                                    
    
    with target.open("x", encoding="utf-8") as file:
        file.write(content)
    return f"已创建文件：{relative_path}"

def execute_tool(name: str, arguments: dict) -> dict:
    if name not in TOOLS_NAME:
        return {
            "is_error": True,
            "content": f"未知工具：{name}",
        }

    if not isinstance(arguments, dict):
        return {
            "is_error": True,
            "content": "工具参数必须是字典",
        }
    
    argument_name = (
        "directory_path" if name == "list_files" else "file_path"
    )
    path = arguments.get(argument_name)
    if not isinstance(path, str) or not path.strip():
        return {
            "is_error": True,
            "content": f"{argument_name} 必须是非空字符串",
        }

    try:
        if name == "read_file":
            content = _read_file(path)
        elif name == "list_files":
            content = _list_files(path)
        else:
            text = arguments.get("content")
            if not isinstance(text, str):
                raise ValueError("content 必须是字符串")

            content = _write_file(path, text)

        return {
            "is_error": False,
            "content": content,
        }
    except (OSError, UnicodeError, ValueError) as error:
        return {
            "is_error": True,
            "content": f"工具执行失败：{error}",
        }