READ_ONLY_TOOLS = {"read_file", "list_files", "grep_search"}
WRITE_TOOLS = {"write_file", "edit_file", "run_shell", "save_memory"}

#name 工具name, mode 权限模式
def check_permission(name: str, mode: str = "default") -> dict:
    if mode not in {"default", "read_only"}:
        return {
            "action": "deny",
            "message": f"未知权限模式：{mode}",
        }

    if name in READ_ONLY_TOOLS:
        return {
            "action": "allow",
            "message": "允许执行只读工具",
        }

    if name in WRITE_TOOLS:
        if mode == "read_only":
            return {
                "action": "deny",
                "message": f"只读模式禁止执行：{name}",
            }

        return {
            "action": "confirm",
            "message": f"执行 {name} 需要用户确认",
        }

    return {
        "action": "deny",
        "message": f"未知工具，拒绝执行：{name}",
    }