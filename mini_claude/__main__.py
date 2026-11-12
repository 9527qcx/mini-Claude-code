import argparse
import asyncio
from pathlib import Path
from .agent import Agent
from openai import APIError
from uuid import uuid4, UUID
from .session import find_latest_session
from httpx import RequestError
from .usage import UsageStats


async def show_answer(agent: Agent, message: str) -> None:
    streamed = False
    completed = False
    def on_text(text: str) -> None:
        nonlocal streamed
        if text:
            if not streamed:
                print("助手：", end="", flush=True)

            streamed = True
            print(text, end="", flush=True)

    try:
        answer = await agent.chat(message, on_text=on_text)

        if not streamed:
            print(answer)

        completed = True

    finally:
        show_usage(agent.turn_usage)

        if agent.context_chars is not None:
            print(
                f"最近准备请求的上下文："
                f"{agent.context_chars} 字符（非 token 数）"
            )

        if not completed:
            print("本轮未完成，用量统计可能不完整。")

def show_usage(stats: UsageStats) -> None:
    calls = stats.reported_requests + stats.missing_requests

    if calls == 0:
        print("暂无已完成调用的用量记录。")
        return

    if stats.reported_requests == 0:
        print(
            f"本轮用量未知："
            f"{stats.missing_requests} 次调用未返回统计。"
        )
        return

    print(
        f"本轮已报告用量："
        f"输入 {stats.prompt_tokens}，"
        f"输出 {stats.completion_tokens}，"
        f"合计 {stats.total_tokens} token"
    )
    print(
        f"有统计的调用：{stats.reported_requests}，"
        f"缺少统计的调用：{stats.missing_requests}"
    )

    if stats.missing_requests:
        print("统计不完整，合计仅包含已返回统计的调用。")

def run_repl(agent: Agent):
    print("输入 exit 退出，/clear 清空历史。")

    while True:
        try:
            msg = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            break

        if not msg:
            continue

        if msg == "exit":
            break

        if msg == "/clear":
            agent.clear_history()
            print("历史已清空。")
            continue

        try:
            asyncio.run(show_answer(agent=agent, message=msg))
        except KeyboardInterrupt:
            print("\n本轮已中断，可以继续输入。")
            continue
        except ValueError as error:
            print(f"配置错误：{error}")
            continue
        except APIError as error:
            print(f"请求失败：{type(error).__name__}")
            continue
        except RequestError as error:
            print(
                f"接收响应失败：{type(error).__name__}，本轮已停止。"
            )
            continue

def main():
    parser = argparse.ArgumentParser(
        description="从零学习实现一个 Coding Agent"
    )
    parser.add_argument("prompt", nargs="?", help="你想告诉助手的话")
    parser.add_argument("--resume",
                        nargs="?",
                        const="latest",
                        default=None,
                        metavar="SESSION_ID",
                        help="恢复指定 ID 的会话",

                    )
    args = parser.parse_args()

    session_dir = Path.cwd() / ".mini-claude" / "sessions"

    try:
        if args.resume is None:
            session_id = uuid4().hex
        elif args.resume == "latest":
            latest_path = find_latest_session(session_dir)
            session_id = latest_path.stem
        else:
            session_id = UUID(args.resume).hex

        session_path = session_dir / f"{session_id}.json"
        agent = Agent(session_path=session_path, enable_memory_recall=True,)

        if args.resume is not None:
            agent.restore_session(session_path)

        print(f"会话 ID：{session_id}")
        if args.prompt is not None:
            asyncio.run(show_answer(agent, args.prompt))
        else:
            run_repl(agent)

    except ValueError as error:
        parser.exit(1, f"错误：{error}\n")
    except APIError as error:
        parser.exit(1, f"请求失败：{type(error).__name__}\n")
    except KeyboardInterrupt:
        print("\n已退出。")
    except RequestError as error:
        parser.exit(
            1,
            f"接收响应失败：{type(error).__name__}，本轮已停止。\n",
        )


if __name__ == "__main__":
    main()
