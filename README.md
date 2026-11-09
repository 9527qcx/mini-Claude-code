# mini-Claude-code

使用 Python 从零学习实现一个命令行 Coding Agent。

## 当前进度

阶段 0 已完成：建立本地仓库并推送到 GitHub。
阶段 1 已完成：实现 CLI 骨架，支持输入回显、帮助和必填参数校验。
阶段 2 已完成：从 .env 加载配置，调用真实模型。
阶段 3 已完成：支持多轮对话、交互输入、/clear 和 exit。
阶段 4 已完成：实现 read_file、list_files 和统一工具执行器。
阶段 5 已完成：实现非流式 Agent Loop、工具调用 ID 配对、工具错误回传和最大模型调用次数限制。
阶段 6 已完成：实现权限检查、文件创建与编辑、代码搜索、PowerShell 执行和超时处理；支持先读后改、唯一匹配与外部修改检测。

## 运行方式

在项目根目录执行：

python -m mini_claude "你好，我正在学习 Agent"

查看帮助：

python -m mini_claude --help

## 验证记录

- 提供 prompt：调用模型并输出回答。
- 不提供 prompt：进入交互模式。
- 使用 --help：显示帮助。
- 多轮对话保留历史，/clear 清空历史，exit 退出。
- read_file 能读取文本，文件不存在时返回错误结果。
- list_files 能列出目录，路径不存在或不是目录时返回错误结果。
- 未知工具返回错误结果。
- 模型能调用工具读取文件、列出目录，并根据结果回答。
- 模拟验证多个工具调用 ID 配对和错误回传。
- 模拟连续工具请求，达到轮次上限后停止。
- 模拟请求中途失败，原有历史保持完整。
- 确认写入后成功创建文件；拒绝后目标文件仍不存在。
- 已有文件不会被覆盖。
- 越界路径和 .git 路径被拒绝。
- 非法内容和父目录不存在时返回错误结果。
- 编辑防护与 Shell 输出测试共 9 项通过。
- 搜索支持正常匹配、无匹配和非法参数检查。
- Shell 输出、退出码和中文捕获正常。
- Shell 超时后终止测试进程树，并清空旧读取记录。

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