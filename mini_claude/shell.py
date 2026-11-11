import os
import subprocess
import tempfile
from pathlib import Path
import asyncio
import time

def _stop_process_tree(process: subprocess.Popen) -> str:
    note = ""

    try:
        cleanup = subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=5,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if cleanup.returncode != 0:
            note = "\n进程树清理未确认"

    except (OSError, subprocess.TimeoutExpired):
        note = "\n进程树清理未确认"

    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    return note

def _wait_process(process: subprocess.Popen, timeout: int) -> None:
    deadline = time.monotonic() + timeout

    try:
        task = asyncio.current_task()#主任务，也就是python代码
    except RuntimeError:
        task = None

    while True:
        if task is not None and task.cancelling():
            raise asyncio.CancelledError()

        if process.poll() is not None:
            return

        remaining = deadline - time.monotonic()

        if remaining <= 0:
            raise subprocess.TimeoutExpired(process.args, timeout)

        try:
            process.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            pass

def run_shell(command: str, timeout: int = 30) -> dict:
    if os.name != "nt":
        raise ValueError("当前 Shell 实现仅支持 Windows")

    if not isinstance(command, str) or not command.strip():
        raise ValueError("command 必须是非空字符串")

    if type(timeout) is not int or not 1 <= timeout <= 120:
        raise ValueError("timeout 必须是 1～120 之间的整数，单位为秒")

    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "[Console]::OutputEncoding = "
        "[System.Text.UTF8Encoding]::new($false)\n"
        "$OutputEncoding = [Console]::OutputEncoding\n"
        "$global:LASTEXITCODE = 0\n"
        "& {\n"
        + command
        + "\n} | Out-String -Stream\nexit $LASTEXITCODE\n"
    )

    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"

    timed_out = False
    cleanup_note = ""

    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                script,
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            cwd=Path.cwd(),
            env=environment,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        try:
            _wait_process(process, timeout)

        except subprocess.TimeoutExpired:
            timed_out = True
            cleanup_note = _stop_process_tree(process)

        except BaseException:
            _stop_process_tree(process)
            raise

        output.seek(0)
        data = output.read(20_001)

    text = data[:20_000].decode("utf-8", errors="replace")
    if not text:
        text = "（无输出）"
    if len(data) > 20_000:
        text += "\n（输出已截断）"

    if timed_out:
        status = f"执行超时：{timeout} 秒"
    else:
        status = f"退出码：{process.returncode}"

    return {
        "is_error": timed_out or process.returncode != 0,
        "content": status + cleanup_note + "\n" + text,
    }
