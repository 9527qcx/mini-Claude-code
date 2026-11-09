import argparse


def main():
    parser = argparse.ArgumentParser(
        description="从零学习实现一个 Coding Agent"
    )
    parser.add_argument("prompt", help="你想告诉助手的话")
    args = parser.parse_args()

    print(f"收到你的请求：{args.prompt}")


if __name__ == "__main__":
    main()