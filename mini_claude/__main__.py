import argparse
import asyncio

from .agent import Agent
from openai import APIError

async def run_repl():
    agent = Agent()
    print("输入 exit 退出，输入 /clear 清空历史。")

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
            answer = await agent.chat(msg)
        except ValueError as error:
            print(f"配置错误：{error}")
            continue
        except APIError as error:
            print(f"请求失败：{type(error).__name__}")
            continue

        print(f"助手：{answer}")

def main():
    parser = argparse.ArgumentParser(
        description="从零学习实现一个 Coding Agent"
    )
    parser.add_argument("prompt", nargs="?", help="你想告诉助手的话")
    args = parser.parse_args()

    try:
        if args.prompt is not None:
            agent = Agent()
            print(asyncio.run(agent.chat(args.prompt)))
        else:
            asyncio.run(run_repl())
    except ValueError as error:
        parser.exit(1, f"配置错误：{error}\n")
    except APIError as error:
        parser.exit(1, f"请求失败：{type(error).__name__}\n")
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()