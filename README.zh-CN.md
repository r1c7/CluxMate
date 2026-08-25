><div align="center">

# CluxMate

**一个 AI 编程智能体 —— 一个 Python 核心，三种前端。**

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows-blue.svg)

[English](README.md) · **中文文档**

</div>

---

## CluxMate 是什么？

CluxMate 是一个 AI 编程智能体：它能阅读你的代码库、规划修改、编辑文件、执行命令、回答问题。一个统一的 Python 核心驱动**三种可互换的前端**：

| 前端 | 你能得到什么 |
|---|---|
| **无头 CLI** | 一次性提示词，适合脚本、CI 与自动化（`cluxmate -p "..."`） |
| **Textual TUI** | 完整的交互式终端界面（`cluxmate`） |
| **Electron 桌面端** | 精美的图形界面，通过 JSON-RPC（stdio）驱动同一个核心 |

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
- **分级风险权限** —— 每个工具声明风险等级（`safe` / `write` / `dangerous`）；四种模式（`plan` / `default` / `acceptEdits` / `yolo`）加上持久化的 always-allow 列表控制审批。`plan` 模式天然只读；危险命令永远需要确认。
- **双层沙箱** —— 文件写入/删除工具由进程内 **WriteFence**（先规范化再包含性检查）守护；模型生成的 `bash` 命令在**操作系统级沙箱**内运行（Windows 低完整性令牌）。沙箱**默认失败即关闭（fail-closed）**，只有 `yolo` 模式——唯一的显式豁免——会解除它。参见 [安全：沙箱](#安全沙箱)。
- **检查点与回滚** —— 每个工作目录都有一个 shadow-git 仓库，在每轮前后快照你的文件，因此可以撤销任意一轮——且是会话级的，其他会话的修改会以冲突形式呈现，绝不会被覆盖。
- **子 agent 委派** —— 把独立任务委派给受限子 agent（`general-purpose`、只读的 `explore`），递归深度上限 4，每个子 agent 都有自己可回放的会话日志。
- **死循环防护** —— 如果 agent 开始重复相同的工具调用，逐级升级的提醒会把它拉回正轨；`MAX_TURNS` 仍是最终的硬兜底。
- **技能、记忆与 MCP** —— 项目级技能包、持久化项目记忆（`AGENTS.md`）、以及 Model Context Protocol 服务器（stdio / HTTP）接入同一条上下文管线。
- **丰富且受控的工具集** —— `bash`、文件读写/编辑/删除、`grep`、`list_dir`、`web_fetch`、`web_search`、`ask_user_question`、子 agent、技能、记忆更新等；每个工具的输出都有上限与截断，保证上下文有界。

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
│  Skills ── Memory ── MCP ── Subagents        │
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

## 安全：沙箱

权限层决定"允许什么"；两层强制边界让"禁止"真正生效：

**① WriteFence（进程内）** —— 守护五个文件工具（`write_file`、`search_replace`、`multi_edit`、`multi_write`、`delete_file`）。每个路径先被规范化（展开 `..`、解析符号链接），再做拒绝检查、包含性检查，*任何 I/O 之前*完成。只有工作目录、平台临时目录和你的 `~/.cluxmate/AGENTS.md` 可写——而 `<项目>/.cluxmate/`（权限配置、MCP 服务器、技能）永远不可写，防止被提示词注入的模型修改自己的权限设置。

**② Bash + MCP 沙箱（OS 级）** —— 模型生成的 `bash` 命令在操作系统沙箱内运行，而不是你的完整用户权限：
- **Windows**：低完整性令牌（`NO_WRITE_UP`），工作区目录树被标记为低完整性——shell 可以读取、可以联网，但无法修改高于其完整性级别的任何内容。
- **失败即关闭**：沙箱开启但后端不可用时，`bash` 拒绝运行，而不是回退到裸子进程。逃生口：`CLUXMATE_BASH_SANDBOX=off`。

两层边界在除 **`yolo`** 之外的每种模式下都开启——`yolo` 是解除一切的唯一显式豁免。可写文件夹授权（`~/.cluxmate/sandbox-grants.json`）允许你白名单额外的目录。权限模式一览：

| 模式 | 行为 | 沙箱 |
|---|---|---|
| `plan` | 只读工具集（写工具根本不注册） | 硬隔离 |
| `default` | `safe` 自动通过；`write` / `dangerous` 需确认 | 开启 |
| `acceptEdits` | `write` 自动通过；`dangerous` 仍需确认 | 开启 |
| `yolo` | 全部自动执行，包括 `dangerous` | **关闭**（豁免） |

<p align="center">
  <img src="snapshots/sandbox.png" alt="桌面端的沙箱与权限视图" width="50%">
</p>

## 子 agent

把独立工作委派给工具集受限的子 agent：

- **`general-purpose`** —— 完整读写/bash 工具集，处理任意子任务。
- **`explore`** —— 只读（`read_file`、`grep`、`list_dir`、`web_fetch`、`web_search`），用于调研。

子 agent 最多递归 **4 层**（达到上限后 `task` 工具被收回），每个子 agent 都是一个子 `SessionLog`，通过 `subagent/spawn` 与父级关联——回放会走完整棵委派树。

<p align="center">
  <img src="snapshots/subagent.png" alt="桌面端的子 agent 树" width="50%">
</p>

## 技能、记忆与 MCP

- **技能（Skills）** —— 项目级指令包（`<项目>/.cluxmate/skills.json`），模型可通过 `use_skill` 工具按需加载。
- **记忆（Memory）** —— 持久化项目记忆文件 `AGENTS.md`，每轮以带标签的合成消息渲染。旧版遗留的 `CLAUDE.md` 文件也会作为只读回退被兼容。
- **MCP** —— Model Context Protocol 服务器（stdio 或 HTTP）把它们的工具直接接入 agent 上下文；服务器每个工作目录只加载一次，在 Windows 上像 `bash` 一样被沙箱保护。


## 安装

### 环境要求

- **Python ≥ 3.12**
- Node.js 18+ 与 npm（桌面端需要）

### 1. Python 包

```bash
git clone https://github.com/<your-account>/cluxmate.git   # 换成真实仓库地址
cd cluxmate

pip install .            # 直接安装
# 或开发模式——可编辑安装（改动即时生效）：
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

- **打包运行** —— 编译并产出 Windows 安装包（`dist/`）：

  ```bash
  npm run package
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

项目级状态（always-allow 权限、MCP 服务器、技能、沙箱授权，以及 hooks——在生命周期时点运行的用户自定义 shell 命令，配置在 `settings.json` 中）位于 `<项目>/.cluxmate/` 下——随项目走，不进home目录。

## 桌面端

Electron 桌面端是同一 Python 核心的全功能前端：

- **会话与工作目录** —— 切换项目、恢复或删除会话、搜索历史会话。
- **Git 集成** —— 当前分支感知、每轮**检查点时间线**、diff 查看器、跨分支的撤销/检出。
- **检视器** —— agent 检视器、上下文查看器（模型看到了什么）、子 agent 树、工具调用卡片、权限卡片。
- **视图** —— hooks、MCP 服务器、技能、设置（含每模型配置，包括推理强度）。
- **托盘应用** —— 常驻系统托盘，带显示/退出菜单。

<p align="center">
  <img src="snapshots/codingwithcluxmate.png" alt="在桌面端中用 CluxMate 写代码" width="50%">
</p>


## 许可证

[MIT](LICENSE) —— 详见 [LICENSE](LICENSE) 文件。
