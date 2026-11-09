# mini-Claude-code

使用 Python 从零学习实现一个命令行 Coding Agent。

## 当前进度

阶段 0 已完成：建立本地仓库并推送到 GitHub。
阶段 1 已完成：实现 CLI 骨架，支持输入回显、帮助和必填参数校验。
阶段 3 已完成：支持多轮对话、交互输入、/clear 和 exit。

## 运行方式

在项目根目录执行：

python -m mini_claude "你好，我正在学习 Agent"

查看帮助：

python -m mini_claude --help

## 验证记录

- 提供 prompt：正确回显输入。
- 使用 --help：显示帮助并正常退出。
- 缺少 prompt：显示参数错误并以非零状态退出。

## 本地环境

- Python 3.12.12
- PowerShell
- Git

## 学习资料

- [Python 复刻学习路线](docs/15-python-rebuild-roadmap.md)
- [GitHub 管理指南](docs/16-github-code-management.md)

## 项目来源

参考 Windy3f3f3f3f 的 claude-code-from-scratch：
https://github.com/Windy3f3f3f3f/claude-code-from-scratch

本仓库按学习阶段逐步实现功能。