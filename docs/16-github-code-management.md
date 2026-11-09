# 用 GitHub 管理复刻代码：Windows 实操指南

配套文档：[Python 复刻学习流程](15-python-rebuild-roadmap.md)。本指南使用 PowerShell，目标是在公开仓库 `mini-Claude-code` 中记录每一个学习步骤。

下面是需要你逐段执行的教程，不表示命令已经执行。先完成第 1～7 节，再开始写第一课代码。

## 1. 先分清 Git 和 GitHub

**Git** 在电脑上记录代码历史，断网也能提交。**GitHub** 托管远程仓库，让你备份、查看和分享这些提交。

```text
编辑文件                 git add              git commit             git push
工作区的修改 ─────────→ 暂存区的修改 ─────────→ 本地历史 ────────────→ GitHub
```

| 操作 | 含义 | 是否上传 GitHub |
|---|---|---|
| 保存文件 | 写到硬盘 | 否 |
| `git add 文件` | 选择本次要提交的内容 | 否 |
| `git commit` | 在本地创建一个版本快照 | 否 |
| `git push` | 将本地提交发送到远程仓库 | 是 |
| `git pull` | 获取远程提交并整合到本地 | 从远程下载 |

## 2. 检查环境

打开 VS Code 的终端，选择 PowerShell：

```powershell
git --version
python --version
```

Python 要求 3.11 及以上。若提示找不到 Git，从 [Git 官方网站](https://git-scm.com/downloads) 安装，再重新打开终端。

浏览器登录自己的 GitHub 账号。提交作者信息与 GitHub 登录是两件事：写好 `user.name` 并不表示已获得推送权限。[GitHub 官方 Git 配置说明](https://docs.github.com/en/get-started/git-basics/set-up-git)

## 3. 建立独立学习目录

当前原项目根目录没有 Git 历史。我们在它下面创建学习目录，只管理逐步复刻的代码。

下面这一段只用于首次创建；如果提示目录已存在，先查看现有内容，不重复执行后续写文件步骤：

```powershell
Set-Location 'E:\vs_ai_workspace\claude-code-from-scratch-main'
if (Test-Path -LiteralPath '.\learning\mini-Claude-code') {
    throw '学习目录已存在，请先检查其中的文件和 Git 状态。'
}
New-Item -ItemType Directory -Path '.\learning\mini-Claude-code\docs' -Force
Copy-Item -LiteralPath '.\docs\15-python-rebuild-roadmap.md' -Destination '.\learning\mini-Claude-code\docs'
Copy-Item -LiteralPath '.\docs\16-github-code-management.md' -Destination '.\learning\mini-Claude-code\docs'
Copy-Item -LiteralPath '.\LICENSE' -Destination '.\learning\mini-Claude-code\LICENSE'
Set-Location '.\learning\mini-Claude-code'
git init -b main
```

复制两份学习文档和许可证即可，`python/mini_claude/` 的完整实现留在参考项目中。文档中的源码路径是参考项目路径，复刻仓库会逐阶段实现对应文件。

检查初始化位置：

```powershell
git rev-parse --show-toplevel
```

预期最后一段是 `learning/mini-Claude-code`，不要在原项目根目录继续提交。

## 4. 设置提交作者

先查看现有设置：

```powershell
git config --get user.name
git config --get user.email
```

如果需要为学习仓库单独设置，先把下面的占位文本换成自己的信息，再执行：

```powershell
git config --local user.name '你的显示名称'
git config --local user.email '你的提交邮箱'
```

本指南使用 `--local`，只影响当前仓库。公开提交会显示作者信息；如果希望使用 GitHub 的隐藏邮箱，在 GitHub 的 Settings → Emails 查看自己专属的 noreply 地址后填写。

## 5. 写好忽略规则和 README

在学习仓库根目录创建 `.gitignore`，内容如下：

```gitignore
# Python 环境与生成文件
.venv/
venv/
__pycache__/
*.py[cod]
*.egg-info/
build/
dist/
.pytest_cache/

# 本地凭据与运行数据
.env
.env.*
!.env.example
.mini-claude/
sessions/
tool-results/
```

只提交不包含真实凭据的 `.env.example`；实际 API Key 放在自己的环境变量或本地配置中。忽略规则不会自动取消已经被 Git 跟踪的文件。

创建 `README.md`，写入：

```markdown
# mini-Claude-code

使用 Python 从零学习实现一个命令行 Coding Agent。

## 当前进度

阶段 0：完成学习路线与版本管理准备，尚未实现 Agent。

## 学习资料

- [复刻学习流程](docs/15-python-rebuild-roadmap.md)
- [GitHub 管理指南](docs/16-github-code-management.md)

## 项目来源

参考 Windy3f3f3f3f 的 claude-code-from-scratch：
https://github.com/Windy3f3f3f3f/claude-code-from-scratch

本仓库按学习阶段逐步实现功能，并保留原项目 MIT 许可证。
```

后续每课更新“当前进度”，加入实际可运行的命令和验证结果。

## 6. 完成第 0 次本地提交

仍在学习仓库根目录执行：

```powershell
git status
git add .gitignore README.md LICENSE docs/15-python-rebuild-roadmap.md docs/16-github-code-management.md
git diff --cached --stat
git diff --cached
```

此时查看暂存内容，确认它只有计划中的文档和配置，再执行：

```powershell
git commit -m "docs: add Python rebuild learning plan"
git log --oneline -3
git status
```

成功标志：能看到刚才的提交，且状态中出现 `working tree clean`。**这时只有本地版本，还没有上传。**

第一次提交前，新文件不会出现在普通 `git diff` 中；用 `git status` 找到它们，加入暂存区后再用 `git diff --cached` 查看内容。

## 7. 新建 GitHub 仓库并首次上传

### 7.1 在网页上创建空仓库

打开 [GitHub 新建仓库页面](https://github.com/new)，按以下内容填写：

| 字段 | 填写内容 |
|---|---|
| Owner | 你自己的账号 |
| Repository name | `mini-Claude-code` |
| Description | `Learn to build a coding agent from scratch in Python` |
| Visibility | Public，按你选择的公开仓库 |
| 初始化 README | 关闭或不勾选 |
| 添加 .gitignore | 不添加 |
| 添加 License | 不添加，本地已经准备 |

点击 **Create repository**。本地已有文件时，创建空远程仓库可避免额外初始化历史造成整合问题。[GitHub 官方创建仓库说明](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository)

### 7.2 关联远程并推送

在新仓库页面选择 HTTPS，复制它显示的仓库地址。把下面的地址换成刚复制的真实地址，不要使用占位用户名：

```powershell
git remote add origin https://github.com/你的用户名/mini-Claude-code.git
git remote -v
git push -u origin main
```

`origin` 是远程仓库的本地别名；`main` 是分支名；`-u` 为首次推送设置上游关联。之后通常直接 `git push` 即可。[GitHub 官方本地代码上传流程](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github?platform=windows)

如果 Git Credential Manager 弹出浏览器登录，按提示完成 GitHub 授权。若出现终端密码提示，不要把 GitHub 账号密码当作 Git 的 HTTPS 凭据；可以使用已配置的凭据管理器，或按官方说明配置个人访问令牌。令牌只在凭据提示中使用，不写进仓库 URL、代码或文档。[GitHub HTTPS 认证说明](https://docs.github.com/en/get-started/git-basics/set-up-git)

刷新 GitHub 仓库页面，核对文件、提交信息及提交短哈希。看到它们才算首次上传完成。

### 7.3 再用终端核对

```powershell
git fetch origin
git status -sb
git rev-parse HEAD
git rev-parse origin/main
```

在本地 `main` 分支上，最后两行哈希相同，说明本地与刚获取的远程 `main` 指向同一个提交。

## 8. 后续每课怎样提交

先完成学习流程中的第一课，并更新 README。以下以 CLI 为例：

```powershell
python -m mini_claude "你好"
python -m mini_claude --help
git status
git diff
git add mini_claude/__init__.py mini_claude/__main__.py README.md
git diff --cached
git commit -m "feat: add CLI skeleton"
git push
```

每次根据实际改动选择文件，不机械照搬示例路径。提交信息写“这次做了什么”，例如：

| 前缀 | 使用场景 | 示例 |
|---|---|---|
| `docs:` | 学习笔记与说明 | `docs: explain the tool loop` |
| `feat:` | 新增能力 | `feat: add read file tool` |
| `fix:` | 修复错误 | `fix: handle missing input files` |
| `refactor:` | 调整结构而不改变功能 | `refactor: extract prompt construction` |
| `test:` | 增加必要验证 | `test: cover denied file edits` |

一次提交解决一件事；运行通过后推送。每课结束在 GitHub 上打开 Commits，再查看该提交具体改了哪些行。

## 9. 用分支试验一个新功能

入门阶段可以先在 `main` 完成几次简单提交。熟悉后，用分支实现独立功能，例如只读工具。

先确认工作区干净，再执行：

```powershell
git switch main
git pull --ff-only
git switch -c feat/read-file
```

完成代码与验证，提交实际变动文件后：

```powershell
git push -u origin feat/read-file
```

在 GitHub 创建 Pull Request，目标分支选 `main`，来源分支选 `feat/read-file`。描述功能、运行命令和结果。查看 Files changed，确认后合并；随后本地同步：

```powershell
git switch main
git pull --ff-only
```

分支是开发中的一条路线；Pull Request 是比较、讨论和合并改动的入口。不要在命令行和网页分别重复合并同一个功能。

## 10. 查看学习历史和标记里程碑

常用查看命令：

```powershell
git log --oneline --graph --decorate -12
git show --stat HEAD
git diff
git diff --cached
```

第一课完成并推送后，可以创建标签，标记这个可运行状态：

```powershell
git tag -a lesson-01 -m "Lesson 01: CLI skeleton"
git push origin lesson-01
```

标签不会自动跟随普通分支推送，因此单独推送。查看那个版本的入口文件，无需切换当前代码：

```powershell
git show lesson-01:mini_claude/__main__.py
```

## 11. 常见问题怎样处理

| 提示或情况 | 含义 | 下一步 |
|---|---|---|
| `not a git repository` | 当前目录不是仓库 | 检查 `Get-Location`，进入学习目录；只有首次准备才初始化 |
| `Author identity unknown` | 缺少作者设置 | 执行第 4 节的本地设置 |
| `nothing to commit` | 没有已暂存的新改动 | 查看 `git status`，确认是否已提交或尚未暂存 |
| `remote origin already exists` | 已有关联 | 先 `git remote -v`，确认地址，不重复添加 |
| `Repository not found` | 地址、账号或访问权限不正确 | 检查真实仓库地址、当前登录与权限 |
| `Authentication failed` | Git 认证失败 | 使用凭据管理器或官方认证流程重新登录 |
| `non-fast-forward` / rejected | 远程有本地没有的提交 | 先 fetch、查看两边历史，再整合；不要直接强制推送 |
| `LF will be replaced by CRLF` | 换行符转换提示 | 通常不妨碍提交，先检查实际 diff |
| `.env` 仍出现在暂存区 | 文件已被跟踪或已加入暂存 | 检查跟踪状态，再按下方处理 |

### 暂存了不该提交的文件

已经有至少一次提交时，可以取消暂存，文件内容仍保留：

```powershell
git restore --staged path/to/file
```

第一个提交之前尚无 `HEAD` 时，可用下面方式仅从索引移除已暂存的文件，保留硬盘上的文件：

```powershell
git rm --cached -- path/to/file
```

路径需替换成实际文件。取消暂存不等于清除历史。如果真实 Key 已上传，先到服务商撤销该 Key；仅增加忽略规则或删除当前文件，不能移除旧提交中的内容。

### 远程比本地新

先确认工作区干净，再查看差异：

```powershell
git fetch origin
git log --oneline --left-right main...origin/main
```

若本地只落后、没有自己的额外提交，可执行 `git pull --ff-only`。如果两边都有提交，先理解差异再选择合并；遇到冲突时逐个文件处理冲突标记、运行验证、暂存并完成合并提交。此时可以把报错交给我逐步解释，不用 `push --force` 或 `reset --hard` 绕过问题。

## 12. 今天只完成这些

- [ ] 建立独立学习目录并确认 Git 根目录。
- [ ] 放入两份学习文档、README、LICENSE 和 `.gitignore`。
- [ ] 创建第 0 次本地提交。
- [ ] 在自己的账号创建公开空仓库 `mini-Claude-code`。
- [ ] 关联真实地址，完成首次 push。
- [ ] 在 GitHub 核对提交，再进入 CLI 第一课。

此后每课重复：**实现 → 运行 → 查看改动 → 暂存 → 提交 → 推送 → 核对远程**。
