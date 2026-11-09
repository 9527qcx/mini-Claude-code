from pathlib import Path
import subprocess

from .shell import run_shell

TOOLS_NAME = ["read_file", "list_files", "write_file", "edit_file", "grep_search", "run_shell"]

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
    {
    "type": "function",
    "function": {
        "name": "edit_file",
        "description": (
            "精确替换文件中的一处文本。必须先 read_file；"
            "old_string 必须非空且唯一匹配；文件变化后需要重新读取"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "old_string": {
                    "type": "string",
                    "description": "需要替换的精确旧文本",
                },
                "new_string": {
                    "type": "string",
                    "description": "替换后的文本，空字符串表示删除",
                },
            },
            "required": ["file_path", "old_string", "new_string"],
            "additionalProperties": False,
        },
      },
    },
    {
    "type": "function",
    "function": {
        "name": "grep_search",
        "description": (
            "递归搜索目录中的 UTF-8 Python 文件，按字面文本匹配，"
            "返回路径、行号和匹配行，最多返回 100 行"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "directory_path": {
                    "type": "string",
                    "description": "项目内的搜索目录",
                },
                "pattern": {
                    "type": "string",
                    "description": "要查找的非空文本，按字面匹配",
                },
            },
            "required": ["directory_path", "pattern"],
            "additionalProperties": False,
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "run_shell",
        "description": (
            "在当前工作目录执行前台 PowerShell 命令，"
            "返回退出码与输出；执行前需要用户确认"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "完整的 PowerShell 命令",
                },
                "timeout": {
                    "type": "integer",
                    "description": "超时秒数，默认 30",
                    "minimum": 1,
                    "maximum": 120,
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
},
]

def _read_file(file_path: str, read_state: dict[str, int] | None = None) -> str:
    path = Path(file_path).resolve()
    key = str(path)

    if read_state is not None:
        read_state.pop(key, None)

    before = path.stat().st_mtime_ns
    content = path.read_bytes().decode("utf-8")
    after = path.stat().st_mtime_ns

    if before != after:
        raise ValueError("读取期间文件发生变化，请重新读取")

    if read_state is not None:
        read_state[key] = after

    return content

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

def _edit_file(
    file_path: str,
    old_string: str,
    new_string: str,
    read_state: dict[str, int] | None,
) -> str:
    workspace = Path.cwd().resolve()
    target = Path(file_path).resolve()

    if not target.is_relative_to(workspace):
        raise ValueError("编辑路径必须位于当前工作目录范围内")

    relative_path = target.relative_to(workspace)
    if any(part.casefold() == ".git" for part in relative_path.parts):
        raise ValueError("禁止编辑 .git 目录")

    key = str(target)

    if read_state is None or key not in read_state:
        raise ValueError("编辑前必须先调用 read_file")

    expected_mtime = read_state[key]

    if target.stat().st_mtime_ns != expected_mtime:
        read_state.pop(key, None)
        raise ValueError("文件已被外部修改，请重新读取")

    content = target.read_bytes().decode("utf-8")

    position = content.find(old_string)
    if position == -1:
        raise ValueError("未找到 old_string，请检查或重新读取")

    if content.find(old_string, position + 1) != -1:
        raise ValueError("old_string 匹配多处，请提供更多上下文")

    updated = content.replace(old_string, new_string, 1)

    if target.stat().st_mtime_ns != expected_mtime:
        read_state.pop(key, None)
        raise ValueError("编辑准备期间文件发生变化，请重新读取")

    read_state.pop(key, None)
    target.write_bytes(updated.encode("utf-8"))

    return f"已编辑文件：{relative_path}；再次编辑前请重新读取"

def _grep_search(directory_path: str, pattern: str) -> str:
    workspace = Path.cwd().resolve()
    root = Path(directory_path).resolve()

    if not root.is_relative_to(workspace):
        raise ValueError("搜索目录必须位于当前工作目录范围内")

    if not root.is_dir():
        raise ValueError("搜索路径不存在或不是目录")

    skipped_dirs = {
        ".git", ".venv", "venv", "__pycache__", "node_modules"
    }
    pending = [root]
    visited = set()
    matches = []

    while pending:
        directory = pending.pop()
        resolved = directory.resolve()

        if resolved in visited:
            continue
        visited.add(resolved)

        for entry in sorted(directory.iterdir()):
            if entry.is_symlink():
                continue

            if not entry.resolve().is_relative_to(root):
                continue

            if entry.is_dir():
                if entry.name.casefold() not in skipped_dirs:
                    pending.append(entry)
                continue

            if not entry.is_file() or entry.suffix.lower() != ".py":
                continue

            text = entry.read_text(encoding="utf-8")
            relative_path = entry.relative_to(workspace).as_posix()

            for line_number, line in enumerate(text.splitlines(), start=1):
                if pattern in line:
                    matches.append(
                        f"{relative_path}:{line_number}:{line}"
                    )

                    if len(matches) >= 100:
                        return "\n".join(matches) + (
                            "\n已达到 100 行上限，可能还有更多匹配"
                        )

    return "\n".join(matches) or "未找到匹配"

def execute_tool(name: str, 
                 arguments: dict, 
                 read_state: dict[str, int] | None = None,) -> dict:
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

    if name == "run_shell":
        try:
            return run_shell(
                arguments.get("command"),
                arguments.get("timeout", 30),
            )
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            return {
                "is_error": True,
                "content": f"Shell 执行失败：{error}",
            }
        finally:
            if read_state is not None:
                read_state.clear()#shell可能修改多个文件，所以调用后清空读取记录
    
    argument_name = (
        "directory_path" 
        if name in {"list_files", "grep_search"} else "file_path"
    )
    path = arguments.get(argument_name)
    if not isinstance(path, str) or not path.strip():
        return {
            "is_error": True,
            "content": f"{argument_name} 必须是非空字符串",
        }

    try:
        if name == "read_file":
            content = _read_file(path, read_state)
        elif name == "list_files":
            content = _list_files(path)
        elif name == "write_file":
            text = arguments.get("content")
            if not isinstance(text, str):
                raise ValueError("content 必须是字符串")
            content = _write_file(path, text)
        elif name == "edit_file":
            old_string = arguments.get("old_string")
            new_string = arguments.get("new_string")

            if not isinstance(old_string, str) or old_string == "":
                raise ValueError("old_string 必须是非空字符串")

            if not isinstance(new_string, str):
                raise ValueError("new_string 必须是字符串")

            content = _edit_file(
                path,
                old_string,
                new_string,
                read_state,
            )
        elif name == "grep_search":
            pattern = arguments.get("pattern")

            if not isinstance(pattern, str) or pattern == "":
                raise ValueError("pattern 必须是非空字符串")
            content = _grep_search(path, pattern)
        else:
            raise ValueError(f"工具尚未接入执行器：{name}")
        return {
            "is_error": False,
            "content": content,
        }
    except (OSError, UnicodeError, ValueError) as error:
        return {
            "is_error": True,
            "content": f"工具执行失败：{error}",
        }