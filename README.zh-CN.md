><div align="center">

# CluxMate

**一个 AI 编程智能体 —— 一个 Python 核心，三种前端。**

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue.svg)

[English](README.md) · **中文文档**

</div>

---

## CluxMate 是什么？

CluxMate 是一个 AI 编程智能体：它能阅读你的代码库、规划修改、编辑文件、执行命令、回答问题。一个统一的 Python 核心驱动**三种可互换的前端**：

| 前端 | 你能得到什么 |
|---|---|
| **无头 CLI** | 一次性提示词，适合脚本、CI 与自动化（`cluxmate -p "..."`） |
| **Textual TUI** | 完整的交互式终端界面（`cluxmate`） |
| **Electron 桌面端** | 交互友好的图形界面，通过 JSON-RPC（stdio）驱动同一个核心 |

它使用 **OpenAI 兼容协议**，因此可以对接 DeepSeek、Qwen、GLM、OpenAI、OpenRouter、Ollama，或任何自建的同协议端点——只需配置一个 `base_url`。没有任何厂商锁定。

## 截图

<p align="center">
  <img src="snapshots/TUI.png" alt="CluxMate Textual TUI" width="48%">
  <img src="snapshots/desktop.png" alt="CluxMate 桌面端" width="48%">
</p>

## 核心亮点

- **一个核心，三种前端** —— 无头 CLI、REPL、Textual TUI 与 Electron 桌面端驱动的是同一个 agent 循环。桌面壳是核心之上的壳层；Python agent 本身完全可以独立使用。
- **无厂商锁定** —— 兼容任意 OpenAI 兼容 API：DeepSeek、Qwen、GLM、OpenAI、OpenRouter 或自建端点。可配置多个模型并随时切换；provider 故障不会让回合崩溃，超时、API 错误和网络故障都会被转译为优雅的、用户可见的消息。
- **事件溯源会话，完全可追溯** —— 每个会话都是一个追加式事件日志；模型的对话历史*由它推导而来*，从不单独存储。每个 agent（主 agent *和*子 agent）的每一轮都被 `turn/start`/`turn/end` 包围，每一步都记录 `step/start`、`request/header` 和工具结果，因此任意一步实际发送的提示词都可以逐字重建、回放；上下文压缩只重写一个摘要区域，不会抹掉底层事件。参见下文 [可回放的会话](#可回放的会话)。
- **稳定、缓存友好的上下文** —— 系统提示词不随会话变化：记忆、技能、模式以带标签的合成消息注入，请求前缀保持稳定，提示词缓存保持热度——UI 中还会展示每轮的缓存命中与延迟指标。
- **分级风险权限** —— 每个工具声明风险等级（`safe` / `write` / `dangerous` / `critical`）；四种模式（`plan` / `default` / `acceptEdits` / `yolo`）加上两套持久化的 always-allow 列表（write 级 + dangerous 级——`delete_file`，以及 `bash` 按类别如 `bash:rm` / `bash:python` / `bash:run`，不支持整体 `bash`）控制审批。运行代码（`python script.py`、`node app.js`、`npm run`、`./x.sh` 等）按 `dangerous` 处理，而不是 `safe`。`plan` 模式天然只读；危险命令除非显式"总是允许"，否则仍需确认，而高危命令（format/mkfs/dd 等）与沙箱提权永远需要逐次确认。
- **双层沙箱** —— 文件写入/删除工具由进程内 **WriteFence**（先规范化再包含性检查）守护；模型生成的 `bash` 命令在**操作系统级沙箱**内运行（Windows 低完整性令牌、Linux bubblewrap、macOS Seatbelt）。沙箱**默认失败即关闭（fail-closed）**，只有 `yolo` 模式——唯一的显式豁免——会解除它。可选的**读黑名单**能把 secrets（`.env`、`*.pem`、`~/.ssh` 等）对模型和 shell/MCP 子进程一起隐藏。参见 [安全：沙箱](#安全沙箱)。
- **网络访问守卫（SSRF）** —— `web_fetch` / `web_search` 在*所有*模式下（包括 `yolo`）都经过 SSRF 守卫：默认拒绝内网/私网地址（RFC1918、loopback、link-local、云 metadata 等），重定向的每一跳都重新校验，DNS 解析失败即关闭。允许/封禁规则可配置（`~/.cluxmate/ssrf.json`），桌面端 Settings → 沙箱 → 网络访问直接管理。参见 [安全：网络访问（SSRF 守卫）](#安全网络访问ssrf-守卫)。
- **网络出口控制（bash/MCP）** —— bash 与 MCP stdio 子进程的出网可以被锁定：`shared`（不受限）、`off`（内核级断网——bwrap `--unshare-net` / Seatbelt `deny network*`）、或 `proxy`（仅白名单可出网，经本地过滤代理）。默认 `shared`；Windows 的 `off` 目前为失败即关闭。参见 [安全：网络出口（bash/MCP）](#安全网络出口bash--mcp)。
- **检查点与回滚** —— 每个工作目录都有一个 shadow-git 仓库，在每轮前后快照你的文件，因此可以撤销任意一轮——且是会话级的，其他会话的修改会以冲突形式呈现，绝不会被覆盖。
- **子 agent 委派** —— 把独立任务委派给工具集受限的子 agent（`general-purpose`、只读的 `explore`，以及一个只审不改、必须给证据的 `reviewer`），也可以用 Markdown 自定义类型。递归深度上限 4，并发有界（同时最多 4 个，其中最多 2 个可写），每个子 agent 都有回合预算、机器可读的状态头和自己可回放的会话日志。
- **代码理解（LSP）** —— 只读的 `lsp` 工具提供 10 种导航操作（跳转定义、查引用、hover、调用层级、诊断等），背后是七套语言服务器配置（Python、TypeScript、JavaScript、Go、Java、Rust、C/C++）。不内置任何服务器：只用你 `PATH` 上已有的；每次编辑后，语言服务器的**错误级诊断会随写工具的结果一起回来**。参见 [代码理解（LSP）](#代码理解lsp)。
- **死循环防护** —— 如果 agent 开始重复相同的工具调用，逐级升级的提醒会把它拉回正轨；`MAX_TURNS` 仍是最终的硬兜底。
- **技能、记忆与 MCP** —— 项目级技能包、持久化项目记忆（`AGENTS.md`）、可选的检索记忆，以及 Model Context Protocol 服务器（stdio / HTTP）接入同一条上下文管线。
- **带 OAuth 的远程 MCP** —— HTTP MCP 服务器可以用 OAuth 2.0 认证（发现 → 动态客户端注册 → loopback 回调上的 PKCE），入口是桌面端 MCP 面板或 `cluxmate mcp auth` / `logout` / `status`。令牌存在 `~/.cluxmate/mcp-auth.json`，对模型的读路径永远拒绝；而且——刻意如此——**不存在任何能触发授权的工具**。参见 [远程 MCP 与 OAuth](#远程-mcp-与-oauth)。
- **生命周期 Hooks** —— 在 `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop` / `SessionStart` / `SessionEnd` / `SubagentStop` / `PreCompact` / `Notification` 时点运行你自己定义的 shell 命令，通过 stdin JSON 收上下文、stdout JSON 决定拦截（block）或注入额外上下文。hooks 是你自己的受信配置，不进沙箱；崩溃/超时一律降级为 no-op。
- **诚实的"完成"** —— agent 自己的总结只是主张，不是证据：每条回复都会与该轮真实执行过的工具调用对账，无据的主张会被打回一次；子 agent 报告 `success` 而它自己的审计不支持时，会被降级为 `partial`。参见 [验证而非轻信](#验证而非轻信完成审计)。
- **丰富且受控的工具集** —— `bash`、文件读写/编辑/删除、`grep`、`list_dir`、`lsp`、`web_fetch`、`web_search`、`ask_user_question`、`todo_write`、子 agent、技能、记忆更新等；每个工具的输出都有上限——超长结果保留头尾预览、其余落盘，而不是淹没上下文。

## 架构一览

```text
┌──────────────────────────────────────────────┐
│                前端                           │
│  CLI ─── REPL ─── Textual TUI ─── Desktop     │
└──────────────────────────────────────────────┘
                     │
         JSON-RPC over stdio（桌面端）
                     │
┌──────────────────────────────────────────────┐
│             Python agent 核心                 │
│  AgentLoop ── SessionLog（事件溯源）           │
│  Builder ── Permissions ── Checkpoints       │
│  WriteFence ── Bash/MCP 沙箱                  │
│  LSP ── Skills ── Memory ── MCP ── Subagents │
│  Hooks ── Grants                              │
└──────────────────────────────────────────────┘
                     │
         OpenAI 兼容 API（httpx）
                     │
┌──────────────────────────────────────────────┐
│  DeepSeek · Qwen · GLM · OpenAI · 任意 base  │
└──────────────────────────────────────────────┘
```

**会话日志是唯一事实来源**：一段追加式事件序列，模型的对话历史由它推导。这让请求前缀保持稳定（对提示词缓存友好）、支持精确回放、并能在崩溃后幸存。

## 可回放的会话

agent 所做的一切都被记录在追加式事件日志中——每个会话一个 `.jsonl` 文件——provider 的对话历史*由这些事件推导*，从不单独存储。这一单一事实来源带来很多好处：

- **每一轮、每一步** —— 每轮被 `turn/start` / `turn/end` 包围；轮内每次模型请求都记录 `step/start` + `request/header`（配置、系统提示词、工具 schema——仅在*变化*时记录），每次工具调用都记录其结果。任意一步实际发送的提示词都可以逐字重建（`session/context` 逐轮展示）。
- **环境变化也会被记录** —— 记忆更新、技能加载、模式切换以带标签的合成 `user/message` 事件记录（`source: memory` / `skill` / `mode`），模式或工具 schema 变化会以 reason `change` 追加新的 `request/header`——因此回放能展示 agent 在任意时点*知道什么、能做什么*，而不只是它说了什么。
- **子 agent 也包含在内** —— 子 agent 只是另一个 agent 循环，带有自己的子 `SessionLog`，通过 `subagent/spawn` 指针与父级关联。回放一个会话会按顺序回放整棵委派树，父与子都包含。
- **压缩而不抹除** —— 当上下文过长时，压缩会把日志的一个区域重写为一条摘要消息（一次 `ReplaceOp`）。底层事件仍保留在追加式日志中，缓存友好的前缀保持完整——没有任何内容被静默丢失。
- **构造上即防崩溃** —— 加载时丢弃断裂的日志尾部；被中断的回合用合成的 `tool/result` + `turn/end {interrupted}` 事件持久收尾，因此重新加载的历史永远是合法转录。撤销通过一次 `truncate` 回退到回合边界。

<p align="center">
  <img src="snapshots/contexthistory.png" alt="桌面端的会话上下文与历史查看器" width="50%">
</p>

## 验证而非轻信：完成审计

一个说"我做完了"的 agent，说的是它相信什么，而不是真的发生了什么。在一条回复被提交之前，CluxMate 会把它与这一轮**真实执行过**的工具调用对账——被拒、参数非法或报错的调用都不算证据：

- **主张必须有依据** —— 声称改了文件，必须有写类工具真的执行过；声称跑了测试或命令，必须有 `bash` 调用，而且只跑了 bash 时，被点名的文件会按修改时间在文件系统上核实。
- **只打回一次，绝不硬拦** —— 无据的主张会以带标签的合成消息**打回给模型一次**（审计是建议性的，永不硬失败一个回合），结果记入日志的 `turn/end.reason.completion_audit`。
- **跨委派一样生效** —— 父 agent 综合的是子 agent 的日志，而不是它的说辞：`turn/end` 异常的子的 `success` 会被降级为 `partial`；每个子 agent 的结果都带机器可读的头（`status`、结束原因、使用回合数、输出 token、耗时）。

意义在于："我做了"与"我以为我做了"是可区分的——在日志里、在子 agent 树里、在父 agent 被告知的内容里。

## 安全：沙箱

权限层决定"允许什么"；下面的强制边界让"禁止"真正生效：

**① WriteFence（进程内）** —— 守护五个文件工具（`write_file`、`search_replace`、`multi_edit`、`multi_write`、`delete_file`）。每个路径先被规范化（展开 `..`、解析符号链接），再做拒绝检查、包含性检查，*任何 I/O 之前*完成。只有工作目录、平台临时目录和你的 `~/.cluxmate/AGENTS.md` 可写——而 `<项目>/.cluxmate/`（权限配置、MCP 服务器、技能）永远不可写，防止被提示词注入的模型修改自己的权限设置。

**② Bash + MCP 沙箱（OS 级）** —— 模型生成的 `bash` 命令在操作系统沙箱内运行，而不是你的完整用户权限，三个平台各有内核级后端：
- **Windows**：低完整性令牌（`NO_WRITE_UP`），工作区目录树被标记为低完整性——shell 可以读取、可以联网，但无法修改高于其完整性级别的任何内容。
- **Linux**：bubblewrap（`bwrap`）挂载命名空间——根文件系统只读，仅工作区/临时目录/授权文件夹可写，`<项目>/.cluxmate` 重新挂载为只读。
- **macOS**：Seatbelt（`sandbox-exec`）——`(allow default)` 基础上只拒绝文件写入，放行工作区/临时目录/授权文件夹，再拒绝 `<项目>/.cluxmate`。
- **失败即关闭**：沙箱开启但后端不可用时，`bash` 拒绝运行，而不是回退到裸子进程。逃生口：`CLUXMATE_BASH_SANDBOX=off`。

MCP stdio 服务器也复用同一沙箱（best-effort：它是用户显式配置，无后端时退回裸运行）。

**③ 读黑名单（可选，用户级）** —— 上面的边界只管*写*；读只有在你要的时候才会被拒。`~/.cluxmate/forbid-read.json`（`{"protect_sensitive": false, "paths": [...]}`）把文件与目录对模型**以及** shell/MCP 子进程一起隐藏：你列出的 `paths`（绝对路径，文件或目录子树），加上——打开 `protect_sensitive` 之后——内置模板覆盖的 `.env`、`.git-credentials`、`.netrc`、磁盘任意位置的 `*.pem` / `*.key` / `*.p12` / `*.pfx`，以及 `~/.ssh`、`~/.aws`、`~/.gnupg` 目录。它在**包括 `yolo` 在内的所有模式**下生效（与 SSRF 守卫同理：它防的是凭据被偷，而不是相信模型意图）；进程级 fence（`read_file` / `grep` / `list_dir`）三平台通用，bwrap/Seatbelt 也在 shell 侧强制目录根——Windows 低完整性只能限写，因此 shell 侧的读禁在那里是有文档记录的 no-op。`~/.cluxmate/mcp-auth.json`（OAuth 令牌）无论开关是否打开都始终禁止读取。

两类写边界在除 **`yolo`** 之外的每种模式下都开启——`yolo` 是解除一切的唯一显式豁免。可写文件夹授权（`~/.cluxmate/sandbox-grants.json`）允许你白名单额外的目录。被沙箱拒绝时，模型可以请求一次性升级（`sandbox_permissions="danger-full-access"` + 一句理由）——这会触发一次 `dangerous` 审批，批准仅对该次调用生效。权限模式一览：

| 模式 | 行为 | 沙箱 |
|---|---|---|
| `plan` | 只读工具集（写工具根本不注册） | 硬隔离 |
| `default` | `safe` 自动通过；`write` / `dangerous` 需确认 | 开启 |
| `acceptEdits` | `write` 自动通过；`dangerous` 仍需确认 | 开启 |
| `yolo` | 全部自动执行，包括 `dangerous` | **关闭**（豁免） |


<p align="center">
  <img src="snapshots/sandbox.png" alt="桌面端的沙箱与权限视图" width="50%">
</p>

## 安全：网络访问（SSRF 守卫）

`web_fetch` / `web_search` 在 agent 进程内以普通网络权限运行——一个被提示词注入的模型可能被诱导去抓取内网服务（loopback 开发服务器、RFC1918 内网主机、云 metadata 端点 `169.254.169.254`）。SSRF 守卫（`cluxmate/tools/_ssrf.py`）在*发起请求前*校验目标地址，是与 `WriteFence` 同级的 T1 类「值」约束——它约束的是模型提供的 URL，而不是拦截恶意代码：

- **默认拒绝内网/私网** —— 内置封禁表覆盖 RFC1918（`10/8`、`172.16/12`、`192.168/16`）、loopback、link-local、云 metadata（`169.254.0.0/16`）、CGNAT、组播、保留段，以及 IPv6 ULA / link-local / 组播 / NAT64 等共 17 个网段，且不可移除。
- **包括 `yolo` 在内的所有模式都生效** —— 这是刻意设计：它防的是远程提示词注入，而非模型意图。
- **重定向每一跳都校验** —— 通过 httpx `event_hooks` 在重定向链的每一跳重新校验，试图绕过的重定向同样会被拦截。
- **DNS 逐 IP 校验、解析失败即关闭** —— 主机名会被解析并检查其每一个 A/AAAA 地址（公网域名解析到 `127.0.0.1` 也会被拦截）；解析失败一律视为不安全而拒绝。
- **allow 优先于一切封禁** —— 规则配置在 `~/.cluxmate/ssrf.json`：`{"allow": [...], "block_extra": [...]}`，条目支持 `host` / `host:port` / `[ipv6]:port` / IP / CIDR。该文件位于 WriteFence 不可写的 `~/.cluxmate/` 内，模型无法修改自己的网络白名单。
- **桌面端可直接管理** —— Settings → 沙箱 → 网络访问；改动立即生效（每次请求重新读取，无需重启）。


## 安全：网络出口（bash / MCP）

`web_fetch` / `web_search` 由上面的 SSRF 守卫负责；这里是与它互补的边界，作用于 **bash 与 MCP stdio 子进程**——它们的出网流量原本不被 OS 沙箱约束。出口模式默认 `shared`（opt-in），保存在 `~/.cluxmate/egress.json`：

- **`shared`**（默认）——网络不受限，即引入出口控制之前的行为。
- **`off`** —— 内核级网络隔离：Linux bwrap 追加 `--unshare-net`（全新网络命名空间，仅 loopback）；macOS Seatbelt 追加 `(deny network*)`。Windows 无法用低完整性令牌实现这一点，因此 `off` 在 Windows 上**失败即关闭**（`bash` 拒绝运行），而不是假装已断网——真正的按进程断网留给阶段 2 的 AppContainer 沙箱。
- **`proxy`** —— 强制流量走一个本地白名单过滤代理（仅绑定 loopback）。白名单**就是** `~/.cluxmate/ssrf.json` 的 `allow` 列表（列表为空则全断，未列出的主机一律拒绝）。代理通过 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY` 注入，因此是 **best-effort**：只约束遵循代理环境变量的客户端。

可在桌面端 Settings → 沙箱 → 网络出口切换，或通过 `egress/config/set` JSON-RPC 方法修改；模式变更会重建 agent，让新模式烘焙进沙箱后端。与其它边界一样，egress 在 `yolo` 模式下关闭。


## 子 agent

把独立工作委派给工具集受限的子 agent：

- **`general-purpose`** —— 完整读写/bash 工具集，处理任意子任务。
- **`explore`** —— 只读（`read_file`、`grep`、`list_dir`、`web_fetch`、`web_search`），用于调研。
- **`reviewer`** —— 审查*已经完成*的工作是否满足它所声称的需求，并给出**逐条主张的结论 + 证据**（测试名、真实命令输出或 `file:line`）——没有证据的 pass 算失败，"看起来实现了"不算证据。它没有文件编辑工具，也不负责修：它的 `bash` 权限只用来跑那条能证明主张的命令。

**也可以自定义**：在 `~/.cluxmate/agents/` 与 `<项目>/.cluxmate/agents/` 下放带 frontmatter（`name`、`description`、`tools`、`model`、`max_turns`、`subagents`）的 Markdown，正文就是该 agent 的指令。

- **深度上限 4** —— 达到上限后 `task` 工具被收回；每个子 agent 都是一个子 `SessionLog`，通过 `subagent/spawn` 与父级关联——回放会走完整棵委派树。
- **并发有界** —— 同时最多 4 个子 agent，其中最多 2 个可写；`task` 调用可以声明 `write_paths`，写入范围重叠的写者会被串行化而不是竞争。
- **有预算、有回报** —— 每个子 agent 都有回合预算，返回结果带机器可读的头部；异常结束却声称 `success` 的会被降级为 `partial`（参见 [验证而非轻信](#验证而非轻信完成审计)）。

<p align="center">
  <img src="snapshots/subagent.png" alt="桌面端的子 agent 树" width="50%">
</p>

## 代码理解（LSP）

grep 只能告诉你一个名字出现在哪里；语言服务器能告诉你它*是什么*。一个只读的 `lsp` 工具提供十种导航操作——`goToDefinition`、`goToDeclaration`、`goToTypeDefinition`、`goToImplementation`、`findReferences`、`hover`、`documentSymbol`、`workspaceSymbol`、`callHierarchy`、`diagnostics`——背后是七套内置语言服务器配置（Python/pyright、TypeScript、JavaScript、Go、Java、Rust、C/C++）。

- **不内置任何服务器** —— 只有当二进制已在你 `PATH` 上时才会启用，否则工具直接返回安装命令（如 `npm i -g pyright`）而不是报错。自动安装是**可选项**：需在 `~/.cluxmate/lsp.json` 或 `<项目>/.cluxmate/lsp.json` 里打开 `auto_install`（与 `mcp.json` 同样深层合并，可自定义服务器与 argv），且 `plan` 模式下永远不会跑安装器。
- **每次编辑后都有诊断** —— 四个写工具会向语言服务器要**错误级**诊断并把它带回工具结果里，模型不用再花一个回合去发现刚弄坏的东西。编辑永远不会顺手装工具链，warnings 刻意丢弃，沉默的服务器也不会卡住写入。
- **构造上只读** —— 十种操作全是查询；安装命令属于你的配置，模型无法自己触发下载。

## 技能、记忆与 MCP

- **技能（Skills）** —— 项目级指令包（`<项目>/.cluxmate/skills.json`），模型可通过 `use_skill` 工具按需加载。
- **记忆（Memory）** —— 持久化项目记忆文件 `AGENTS.md`，每轮以带标签的合成消息渲染。旧版遗留的 `CLAUDE.md` 文件也会作为只读回退被兼容。
- **检索记忆（可选）** —— `remember` / `forget` 工具把跨会话事实存成一条条 Markdown，每轮按词法相关性自动召回并注入。事实分**项目级**（默认）与**全局**两种作用域；默认关闭，`~/.cluxmate/retrieval-memory.json`。
- **MCP** —— Model Context Protocol 服务器（stdio 或 HTTP）把它们的工具直接接入 agent 上下文；服务器每个工作目录只加载一次，stdio 服务器像 `bash` 一样被操作系统沙箱保护。走 OAuth 的远程服务器见[下文](#远程-mcp-与-oauth)。

## 远程 MCP 与 OAuth

远程（HTTP）MCP 服务器有三种认证来源，优先级如下：

1. **`mcp.json` 里显式的 `oauth`** —— 优先于其它一切：

   ```json
   {
     "mcpServers": {
       "linear": {
         "url": "https://mcp.linear.app/mcp",
         "oauth": { "client_id": "…", "client_secret_env": "LINEAR_MCP_SECRET", "scopes": "read write", "callback_port": 0 }
       }
     }
   }
   ```

   每个字段都可选：不给 `client_id` 就走动态客户端注册；`client_secret_env` 存的是环境变量*名*；`oauth: false` 显式关闭；`oauth: true` 用默认值开启。
2. **静态 `Authorization` 头**（或 `authorization_env`）—— 你自己写下的凭据按原样使用。
3. **潜伏 OAuth** —— 远程服务器没有静态头时，若已存有令牌就直接用；`401` 才会把登录入口露出来（永不预先弹浏览器）。

- **两种前端都能登录/登出** —— 桌面端 MCP 面板对待授权的服务器显示「需要登录」与登录按钮，认证后显示过期时间与登出；CLI 同样能无头操作：

  ```bash
  cluxmate mcp status                 # 服务器列表 + 认证状态
  cluxmate mcp auth linear            # 打开浏览器，等待 loopback 回调
  cluxmate mcp logout linear          # 删除已存凭据
  ```

- **令牌拿不到** —— 凭据以 `0600` 写入 `~/.cluxmate/mcp-auth.json`，对读工具永远拒绝；绑定到某个 URL 的令牌在端点变化后不会复用。模型侧**没有任何**工具能发起授权，只有你的动作可以。
- **自动刷新，失败要诚实** —— 过期的令牌若有 refresh token 就静默刷新；刷新被拒则删除凭据（瞬时失败则保留）。`needs_auth`（你有动作要做）与 `failed`（去看错误）是刻意分开的两个状态。每次调用至多强制刷新并重试一次。
- **OAuth 只用于远程服务器** —— 在 stdio 服务器上写 `oauth` 是配置错误，不会被静默忽略。


## 生命周期 Hooks

在固定时点运行你自己定义的 shell 命令（Claude Code 风格）。命令通过 stdin JSON 接收上下文，通过 stdout JSON 决定拦截或注入：

| 事件 | 触发时机 | 可拦截 | 可注入 |
|---|---|---|---|
| `UserPromptSubmit` | 提示词送达模型之前 | ✓ | ✓ |
| `PreToolUse` | 工具执行前（审批之后） | ✓（拒绝该工具） | ✓ |
| `PostToolUse` | 工具执行后 | — | ✓ |
| `Stop` | 回复生成后、提交前 | ✓（重跑模型，最多 3 次） | ✓ |
| `SessionStart` | 每次会话启动 | ✓（中止启动） | ✓（仅第一轮） |
| `SessionEnd` | 正常退出 / 切会话 / REPL `/clear` | —（输出丢弃） | — |
| `SubagentStop` | 子 agent 结束后 | ✓（替换其回复） | ✓ |
| `PreCompact` | 自动压缩前 | ✓（跳过本次） | ✓ |
| `Notification` | 回合结束（桌面端）+ `hooks/notify` RPC / "Test notify" | —（发完即忘） | — |

```jsonc
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [{"type": "command", "command": "python .cluxmate/hooks/audit.py", "timeout": 30}]
      }
    ]
  }
}
```

- **拦截**：输出 `{"decision":"block","reason":"..."}` 或以退出码 2 结束 → 该工具/回复被阻止，模型收到 reason。
- **注入**：输出 `{"hookSpecificOutput":{"additionalContext":"..."}}` → 额外上下文注入给模型。
- **位置**：`~/.cluxmate/settings.json`（全局）与 `<项目>/.cluxmate/settings.json`（项目，后运行）合并生效。事件专属的 payload 字段（`source`、`reason`、`subagent_id`、`trigger`、`message` 等）随着 stdin JSON 一起传入。
- **信任模型**：hooks 是你自己的受信配置，运行在普通子进程里（不进沙箱）；崩溃/超时一律降级为 no-op。改动在桌面端 Hooks 视图点 Reload 生效（CLI/TUI 重启生效）。

## 安装

### 环境要求

- **Python ≥ 3.12**
- Node.js 18+ 与 npm（桌面端需要）

### 1. Python 包

```bash
git clone https://github.com/r1c7/CluxMate.git   
cd cluxmate

pip install .            # 直接安装
# 或开发模式——可编辑安装（源码改动即时生效）：
pip install -e .
pip install pytest pytest-asyncio   # 开发依赖不在 pyproject.toml 中
```

安装完成后，`cluxmate` 命令即可用（已加入 `PATH`）。

### 2. 桌面端

桌面端通过 `cluxmate agent stdio` 驱动 Python agent，所以**请先安装 Python 包**并确保 `cluxmate` 在你的 `PATH` 中。

```bash
cd desktop
npm install
```

然后按需选择运行方式：

- **开发模式** —— 带热重载启动应用，适合开发桌面端代码：

  ```bash
  npm run dev
  ```

- **打包运行** —— 编译并产出当前平台的安装包（`dist/`）：

  ```bash
  npm run package          # 当前平台
  npm run package:win      # Windows 安装包
  npm run package:mac      # macOS DMG
  npm run package:linux    # Linux AppImage / deb
  ```

其他常用命令：

```bash
npm run build      # 仅编译（electron-vite build）
npm run preview    # 预览编译产物
npm run typecheck  # 类型检查 main + renderer
```

## 使用

```bash
cluxmate -p "解释一下会话日志的设计"                    # 无头一次性执行
cluxmate -p "重构这个" --model-id deepseek              # 指定某个模型条目
cluxmate -p "..." --reasoning-effort high               # 指定推理强度（按方言）
cluxmate repl                                           # 交互式 REPL
cluxmate                                                # Textual TUI
cluxmate agent stdio                                    # JSON-RPC stdio 服务器（桌面端后端）

cluxmate mcp status                                     # MCP 服务器 + 认证状态
cluxmate mcp auth linear                                # 为远程 MCP 服务器做 OAuth 登录
cluxmate mcp logout linear                              # 删除已存的 MCP 凭据
```

运行一次 `cluxmate` 会生成默认配置，然后在 TUI/桌面端设置里选择模型。

## 配置

全局配置位于 `~/.cluxmate/config.json`（schema v2）——一个模型*条目*列表加上当前激活模型：

```json
{
  "version": 2,
  "models": [
    {
      "id": "deepseek",
      "api_type": "openai",
      "provider": "DeepSeek",
      "base_url": "https://api.deepseek.com",
      "api_key": "",
      "model_name": "deepseek-v4-flash",
      "context_1m": false,
      "max_tokens": 80000,
      "reasoning_efforts": ["low", "high", "max"]
    }
  ],
  "active_model_id": "deepseek"
}
```

- **`base_url`** —— 任意 OpenAI 兼容端点（DeepSeek、Qwen、GLM、OpenAI、OpenRouter、Ollama、自建…）。
- **`api_key`** —— 可以留空；会回退到 `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` 环境变量。
- **`context_1m`** —— 对支持 1M token 上下文的 provider 开启。
- **`max_tokens`** —— 输出预算；留空/`0` 时使用 32768 默认值。
- **`reasoning_efforts`** —— 可选的每模型推理强度覆盖（系统内置各方言预设：DeepSeek / Qwen / GLM / OpenAI）。

项目级状态位于 `<项目>/.cluxmate/` 下——随项目走，不进 home 目录：

| 作用域 | 文件 |
|---|---|
| **项目**（`<项目>/.cluxmate/`） | `permissions.json`（always-allow 列表）、`mcp.json`、`lsp.json`、`settings.json`（[hooks](#生命周期-hooks)）、`skills/`（技能包）+ `skills.json`（哪些副本被禁用）、`agents/`（自定义子 agent 类型）、`memory/facts/`（检索事实），以及 `tmp-spill/` 等临时目录 |
| **全局**（`~/.cluxmate/`） | `config.json`（模型）、`AGENTS.md`（全局记忆）、`sandbox-grants.json`（可写文件夹授权）、`forbid-read.json`（读黑名单）、`ssrf.json`（网络允许/封禁）、`egress.json`（bash/MCP 出网模式）、`retrieval-memory.json`、`mcp-auth.json`（OAuth 令牌）、`checkpoints/`，以及双侧配置族的全局半边——`mcp.json`、`lsp.json`、`settings.json`、`agents/`、`skills/`、`memory/global/facts/` |

两侧都有半边的配置族按全局 → 项目解析：`mcp.json`、`lsp.json` 与自定义子 agent 类型由项目侧覆盖全局侧，而 hooks 是**累加**的——全局的先跑，然后跑项目的。模型绝不该能改的寄存器——可写文件夹授权、读黑名单、SSRF 规则、出网模式——刻意只放在**用户级**，在有提示词注入的模型够不着的地方。

## 桌面端

Electron 桌面端是同一 Python 核心的全功能前端：

- **会话与工作目录** —— 切换项目、恢复或删除会话、搜索历史会话。
- **Git 集成** —— 当前分支感知、每轮**检查点时间线**、diff 查看器、跨分支的撤销/检出。
- **检视器** —— agent 检视器、上下文查看器（模型看到了什么）、子 agent 树、工具调用卡片、权限卡片，以及由 agent 自己的 `todo_write` 驱动的实时任务列表面板。
- **视图** —— hooks、MCP 服务器（带 OAuth 登录/登出与「需要登录」标识）、技能、设置（每模型配置含推理强度，沙箱文件夹授权，读黑名单与敏感文件开关，网络访问规则与出网模式，记忆事实，主题、字体、语言、通知行为）。
- **只在必要时打扰你** —— 有提示词在等你时任务栏图标会闪烁（macOS 上是 critical 停靠栏弹跳）。
- **托盘应用** —— 常驻系统托盘，带显示/退出菜单。

<p align="center">
  <img src="snapshots/codingwithcluxmate.png" alt="在桌面端中用 CluxMate 写代码" width="50%">
</p>


## 许可证

[MIT](LICENSE) —— 详见 [LICENSE](LICENSE) 文件。
