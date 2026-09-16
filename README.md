><div align="center">

# CluxMate

**An AI coding agent — one Python core, three front-ends.**

![Python](https://img.shields.io/badge/python-3.12+-blue.svg)
![License](https://img.shields.io/badge/license-MIT-green.svg)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-blue.svg)

**English** · [中文文档](README.zh-CN.md)

</div>

---

## What is CluxMate?

CluxMate is an AI coding agent that reads your codebase, plans changes, edits files, runs commands, and answers questions. A single Python core powers **three interchangeable front-ends**:

| Front-end | What you get |
|---|---|
| **Headless CLI** | One-shot prompts for scripts, CI, and automation (`cluxmate -p "..."`) |
| **Textual TUI** | A full interactive terminal UI (`cluxmate`) |
| **Electron desktop** | A polished GUI that drives the same core over JSON-RPC (stdio) |

It speaks the **OpenAI-compatible API**, so it works with DeepSeek, Qwen, GLM, OpenAI, OpenRouter, Ollama, or any self-hosted endpoint using the same protocol — just point it at a `base_url`. No vendor lock-in.

## Screenshots

<p align="center">
  <img src="snapshots/TUI.png" alt="CluxMate Textual TUI" width="48%">
  <img src="snapshots/desktop.png" alt="CluxMate Desktop" width="48%">
</p>

## Highlights

- **One core, three front-ends** — the headless CLI, the REPL, the Textual TUI, and the Electron desktop all drive the exact same agent loop. The desktop app is a shell around that core; the Python agent remains fully usable on its own.
- **No vendor lock-in** — speaks any OpenAI-compatible API: DeepSeek, Qwen, GLM, OpenAI, OpenRouter, or a self-hosted endpoint. Configure multiple models and switch on the fly; provider failures never crash a turn, because timeouts, API errors, and network failures are translated into graceful, user-visible messages.
- **Event-sourced sessions, fully traceable** — every session is an append-only event log; the model's message history is *derived* from it, never stored separately. Every turn of every agent — main *and* subagents — is bracketed by `turn/start`/`turn/end`, every step logs `step/start`, `request/header`, and tool results, so the exact prompt sent at any step can be reconstructed and replayed verbatim, and context compaction rewrites a summary region without erasing the underlying events. See [Sessions you can replay](#sessions-you-can-replay) below.
- **Stable, cache-friendly context** — the system prompt never changes with your session: memory, skills, and mode are injected as tagged synthetic messages, so request prefixes stay stable and prompt caches stay hot — with per-turn cache-hit and latency metrics surfaced in the UI.
- **Risk-tiered permissions** — every tool declares a risk level (`safe` / `write` / `dangerous` / `critical`); four modes (`plan` / `default` / `acceptEdits` / `yolo`) plus two persistent always-allow lists (write tier + dangerous tier — `delete_file`, and `bash` per category like `bash:rm` / `bash:python` / `bash:run`, never whole-tool) control approval. Running code (`python script.py`, `node app.js`, `npm run`, `./x.sh`, …) is `dangerous`, not `safe`. `plan` mode is read-only by construction; dangerous commands prompt unless explicitly always-allowed, and critical commands (format/mkfs/dd, etc.) and sandbox escalation always prompt.
- **A two-layer sandbox** — file write/delete tools are guarded by an in-process **WriteFence** (canonicalize-then-contain), and model-generated `bash` commands run inside an **OS-level sandbox** (Windows Low-integrity token, Linux bubblewrap, macOS Seatbelt). Sandboxing is *fail-closed* and only `yolo` mode — the explicit opt-out — disarms it. An opt-in **read denylist** hides secrets (`.env`, `*.pem`, `~/.ssh`, …) from the model and from shell/MCP subprocesses. See [Security: sandbox](#security-sandbox).
- **Network-access guard (SSRF)** — `web_fetch` / `web_search` pass through an SSRF guard in *every* mode (including `yolo`): internal/private addresses (RFC1918, loopback, link-local, cloud metadata, …) are denied by default, every redirect hop is re-validated, and DNS resolution failures close the request. Allow/block rules are configurable (`~/.cluxmate/ssrf.json`), managed from desktop Settings → Sandbox → Network access. See [Security: network access (SSRF guard)](#security-network-access-ssrf-guard).
- **Network egress control (bash/MCP)** — bash and MCP stdio subprocesses can have their outbound traffic locked down: `shared` (unrestricted), `off` (kernel-level deny — bwrap `--unshare-net` / Seatbelt `deny network*`), or `proxy` (allowlist-only via a local filtering proxy). Default `shared`; Windows `off` is fail-closed for now. See [Security: network egress (bash/MCP)](#security-network-egress-bash--mcp).
- **Checkpoints & rewind** — a shadow-git repository per working directory snapshots your files before and after every turn, so you can undo any turn — session-scoped, so other sessions' edits surface as conflicts, never clobbered.
- **Subagent delegation** — delegate independent tasks to restricted subagents (`general-purpose`, read-only `explore`, and an evidence-gated `reviewer` that fixes nothing), or define your own in Markdown. Recursion is capped at depth 4, parallelism is bounded (4 at once, 2 of them writers), and every child gets a turn budget, a machine-readable status header, and its own replayable session log.
- **Code intelligence (LSP)** — a read-only `lsp` tool exposes 10 navigation operations (go-to-definition, find-references, hover, call hierarchy, diagnostics, …) across seven language-server specs (Python, TypeScript, JavaScript, Go, Java, Rust, C/C++). Nothing is bundled: servers come from your `PATH`, and after every edit the language server's **error-level diagnostics ride back in the write tool's result**. See [Code intelligence (LSP)](#code-intelligence-lsp).
- **Doom-loop guard** — if the agent starts repeating identical tool calls, escalating advisories nudge it back on track; `MAX_TURNS` remains the hard backstop.
- **Skills, memory & MCP** — project-scoped skill packs, durable project memory (`AGENTS.md`), opt-in retrieval memory, and Model Context Protocol servers (stdio / HTTP) plug into the same context pipeline.
- **Remote MCP with OAuth** — HTTP MCP servers can authenticate with OAuth 2.0 (discovery → dynamic client registration → PKCE on a loopback callback), driven from the desktop MCP panel or `cluxmate mcp auth` / `logout` / `status`. Tokens live in `~/.cluxmate/mcp-auth.json`, are always denied to the model's read path, and — deliberately — **no tool exists that can trigger authorization**. See [Remote MCP servers & OAuth](#remote-mcp-servers--oauth).
- **Lifecycle hooks** — run your own shell commands at `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop` / `SessionStart` / `SessionEnd` / `SubagentStop` / `PreCompact` / `Notification`, receiving context as stdin JSON and deciding to block or inject context via stdout JSON. Hooks are your own trusted config, never sandboxed; crashes/timeouts degrade to a no-op.
- **Honest completion** — an agent's own summary is a claim, not evidence: every reply is reconciled against the tool calls that actually ran, and an unsupported claim is bounced back once. A subagent that reports `success` while its own audit says otherwise is downgraded to `partial`. See [Trust, but verify](#trust-but-verify-the-completion-audit).
- **Rich toolset, safely capped** — `bash`, file read/write/edit/delete, `grep`, `list_dir`, `lsp`, `web_fetch`, `web_search`, `ask_user_question`, `todo_write`, subagents, skills, memory updates and more; every tool's output is bounded — an oversized result keeps a head/tail preview and spills the rest to disk instead of flooding the context.

## Architecture at a glance

```text
┌──────────────────────────────────────────────┐
│                 Front-ends                   │
│  CLI ─── REPL ─── Textual TUI ─── Desktop    │
└──────────────────────────────────────────────┘
                     │
         JSON-RPC over stdio (desktop)
                     │
┌──────────────────────────────────────────────┐
│             Python agent core                │
│  AgentLoop ── SessionLog (event-sourced)     │
│  Builder ── Permissions ── Checkpoints       │
│  WriteFence ── Bash/MCP sandbox              │
│  LSP ── Skills ── Memory ── MCP ── Subagents │
│  Hooks ── Grants                              │
└──────────────────────────────────────────────┘
                     │
         OpenAI-compatible API (httpx)
                     │
┌──────────────────────────────────────────────┐
│  DeepSeek · Qwen · GLM · OpenAI · any base   │
└──────────────────────────────────────────────┘
```

The **session log is the source of truth**: an append-only sequence of events from which the model's message history is derived. This keeps request prefixes stable (prompt-cache friendly), enables exact replay, and survives crashes.

## Sessions you can replay

Everything the agent does is recorded in an append-only event log — one `.jsonl` file per session — and the provider message history is *derived* from those events, never stored separately. That single source of truth buys a lot:

- **Every turn, every step** — each turn is bracketed by `turn/start` / `turn/end`; inside it, every model request logs `step/start` + `request/header` (config, system prompt, tool schemas — only when they *change*), and every tool call logs its result. The exact prompt sent at any step can be reconstructed verbatim (`session/context` shows it turn by turn).
- **Environment changes are logged too** — memory updates, skill loads, and mode switches are recorded as tagged synthetic `user/message` events (`source: memory` / `skill` / `mode`), and a mode or tool-schema change appends a fresh `request/header` with reason `change` — so replay shows *what the agent knew and could do* at every point, not just what it said.
- **Subagents included** — a subagent is just another agent loop with its own child `SessionLog`, linked to its parent via `subagent/spawn` pointers. Replaying a session replays the whole delegation tree, parents and children, in order.
- **Compaction without erasure** — when a context grows too long, compaction rewrites one region of the log into a single summary message (one `ReplaceOp`). The underlying events stay in the append-only log and the prompt-cache-friendly prefix stays intact — nothing is silently lost.
- **Crash-safe by construction** — a torn tail is dropped on load, and an interrupted turn is durably closed with synthetic `tool/result` + `turn/end {interrupted}` events, so a reloaded history is always a valid transcript. Undo rewinds to a turn boundary via a single `truncate`.

<p align="center">
  <img src="snapshots/contexthistory.png" alt="Session context & history viewer in the desktop app" width="50%">
</p>

## Trust, but verify: the completion audit

An agent that says "done" has told you what it believes, not what happened. Before a reply is committed, CluxMate reconciles it against the tool calls that actually ran in that turn — a denied, malformed, or errored call doesn't count as evidence:

- **Claims need backing** — a claim that files changed requires a write tool to have run; a claim that tests or a command ran requires a `bash` call, and when only `bash` ran the named files are checked against the filesystem by modification time.
- **Bounced once, never blocked** — an unsupported claim is refuted back to the model **once**, as a tagged synthetic message (the audit is advisory; it never hard-fails a turn), and the outcome lands in the log as `turn/end.reason.completion_audit`.
- **It applies across delegation** — the parent synthesizes its own view from the child's log rather than from the child's prose: a child whose `turn/end` was abnormal gets its `success` downgraded to `partial`, and every child report carries a machine-readable header (`status`, end reason, turns used, output tokens, elapsed).

The point is that "I did it" and "I believe I did it" are distinguishable — in the log, in the subagent tree, and in what the parent is told.

## Security: sandbox

Permissions decide what is *allowed*; the enforcement boundaries below make denials *stick*:

**① WriteFence (in-process)** — guards the five file tools (`write_file`, `search_replace`, `multi_edit`, `multi_write`, `delete_file`). Every path is canonicalized (`..` and symlinks resolved) then checked: deny-list first, containment second, *before any I/O*. Only the working directory, the platform temp dir, and your `~/.cluxmate/AGENTS.md` are writable — and `<project>/.cluxmate/` (permission config, MCP servers, skills) is always off-limits so a prompt-injected model can never edit its own permission settings.

**② Bash + MCP sandbox (OS-level)** — model-generated `bash` commands run under an OS sandbox instead of your full user, with a kernel-level backend on each platform:
- **Windows**: a Low-integrity token (`NO_WRITE_UP`) with the workspace tree labeled low — the shell can read and reach the network, but cannot modify anything above its integrity level.
- **Linux**: bubblewrap (`bwrap`) mount namespaces — the root filesystem is read-only, only the workspace/temp dirs/granted folders are writable, and `<project>/.cluxmate` is re-mounted read-only.
- **macOS**: Seatbelt (`sandbox-exec`) — `(allow default)` with only file writes denied, the workspace/temp dirs/granted folders allowed, and `<project>/.cluxmate` denied again.
- **Fail-closed**: if sandboxing is on but no backend is available, `bash` refuses to run rather than falling back to a bare subprocess. Escape hatch: `CLUXMATE_BASH_SANDBOX=off`.

MCP stdio servers reuse the same sandbox (best-effort: they are your explicit config, so with no backend they fall back to running bare).

**③ Read denylist (opt-in, user-global)** — the fences above stop *writes*; reads are denied only if you ask. `~/.cluxmate/forbid-read.json` (`{"protect_sensitive": false, "paths": [...]}`) hides files and directories from the model **and** from shell/MCP subprocesses: the `paths` you list (absolute, files or directory subtrees), plus — once you flip `protect_sensitive` — a built-in template covering `.env`, `.git-credentials`, `.netrc`, `*.pem` / `*.key` / `*.p12` / `*.pfx` anywhere on disk, and the `~/.ssh`, `~/.aws`, `~/.gnupg` directories. It is enforced in **every** mode including `yolo` (like the SSRF guard, it defends against credential theft rather than trusting model intent); the process-level fence (`read_file` / `grep` / `list_dir`) covers all three platforms, and bwrap/Seatbelt enforce the directory roots shell-side too — on Windows Low-integrity can only restrict writes, so the shell-side read deny is a documented no-op there. `~/.cluxmate/mcp-auth.json` (OAuth tokens) is always denied, toggle or not.

Both write boundaries are enabled in every mode **except `yolo`** — the one explicit opt-out that disarms everything. Writable-folder grants (`~/.cluxmate/sandbox-grants.json`) let you whitelist extra directories. When the sandbox denies something, the model can request a one-off escalation (`sandbox_permissions="danger-full-access"` + a one-sentence reason) — this triggers a `dangerous` approval, and the grant applies to that single call only. The permission modes:

| Mode | Behavior | Sandbox |
|---|---|---|
| `plan` | Read-only toolset (writes aren't even registered) | Hard isolation |
| `default` | `safe` auto-approves; `write` / `dangerous` prompt | On |
| `acceptEdits` | `write` auto-approves; `dangerous` still prompts | On |
| `yolo` | Everything auto-approves, including `dangerous` | **Off** (opt-out) |


<p align="center">
  <img src="snapshots/sandbox.png" alt="Sandbox & permissions in the desktop app" width="50%">
</p>

## Security: network access (SSRF guard)

`web_fetch` / `web_search` run in the agent process at normal network privileges — a prompt-injected model could be steered into fetching internal services (loopback dev servers, RFC1918 hosts, the cloud metadata endpoint at `169.254.169.254`). The SSRF guard (`cluxmate/tools/_ssrf.py`) validates the destination *before the request is made*; it is a T1-class "value" constraint, in the same trust model as `WriteFence` — it constrains the URL the model supplies, not malicious code:

- **Internal/private denied by default** — the built-in deny table covers RFC1918 (`10/8`, `172.16/12`, `192.168/16`), loopback, link-local, cloud metadata (`169.254.0.0/16`), CGNAT, multicast, and reserved ranges, plus IPv6 ULA / link-local / multicast / NAT64 — 17 networks in all, and they cannot be removed.
- **Enforced in every mode, including `yolo`** — deliberate: it defends against remote prompt-injection, not model intent.
- **Every redirect hop re-validated** — httpx `event_hooks` re-checks each hop of a redirect chain, so a redirect that tries to slip past is still blocked.
- **DNS checked per-IP, fail-closed** — a hostname is resolved and every A/AAAA address is checked (a public name resolving to `127.0.0.1` is blocked); a resolution failure is treated as unsafe and refused.
- **`allow` wins over every block** — rules live in `~/.cluxmate/ssrf.json`: `{"allow": [...], "block_extra": [...]}`, with entries of `host` / `host:port` / `[ipv6]:port` / IP / CIDR. The file sits inside the WriteFence non-writable `~/.cluxmate/`, so the model cannot edit its own network allowlist.
- **Manage it from the desktop** — Settings → Sandbox → Network access; changes take effect immediately (re-read on every request, no restart).


## Security: network egress (bash / MCP)

`web_fetch` / `web_search` are guarded by the SSRF guard above; this is the complementary boundary for **bash and MCP stdio subprocesses**, whose outbound traffic the OS sandbox otherwise leaves unrestricted. Egress is opt-in (`shared` by default) and lives in `~/.cluxmate/egress.json`:

- **`shared`** (default) — network unrestricted, exactly the pre-egress behavior.
- **`off`** — kernel-level network isolation: Linux bwrap adds `--unshare-net` (a fresh network namespace, loopback only); macOS Seatbelt adds `(deny network*)`. Windows cannot do this with a Low-integrity token, so `off` on Windows **fails closed** (`bash` refuses to run) rather than pretending to be offline — real per-process denial is deferred to the phase-2 AppContainer sandbox.
- **`proxy`** — forces traffic through a local allowlist-filtering proxy (loopback-only). The allowlist **is** the `allow` list from `~/.cluxmate/ssrf.json` (an empty list blocks everything, and unlisted hosts are denied). The proxy is injected via `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`, so it's **best-effort**: only proxy-honoring clients are constrained.

Change it from desktop Settings → Sandbox → Network egress, or via the `egress/config/set` JSON-RPC method; a mode change rebuilds the agent so the new mode is baked into the sandbox backend. Like the other boundaries, egress is off in `yolo` mode.


## Subagents

Delegate independent work to child agents with restricted toolsets:

- **`general-purpose`** — the full read/write/bash toolset for any sub-task.
- **`explore`** — read-only (`read_file`, `grep`, `list_dir`, `web_fetch`, `web_search`) for research.
- **`reviewer`** — judges work that is *already done* against the requirement it claims to satisfy, and returns a **per-claim verdict with evidence** (a test name, real command output, or `file:line`) — a pass asserted without evidence is a failure, and "looks implemented" is not evidence. It holds no file-editing tools and fixes nothing: its `bash` access exists to run the command that proves a claim.

**Define your own** as Markdown files with frontmatter (`name`, `description`, `tools`, `model`, `max_turns`, `subagents`) under `~/.cluxmate/agents/` and `<project>/.cluxmate/agents/`; the body becomes that agent's instructions.

- **Depth cap 4** — the `task` tool is withheld at the cap; each subagent is a child `SessionLog` linked to its parent via `subagent/spawn`, and replay walks the whole delegation tree.
- **Bounded parallelism** — at most 4 subagents run concurrently and at most 2 of them may write; a `task` call can declare `write_paths`, and writers whose scopes overlap are serialized instead of racing.
- **Budgeted and reported** — every child gets a turn budget, and its result comes back with a machine-readable header, with a claimed `success` on an abnormal end downgraded to `partial` (see [Trust, but verify](#trust-but-verify-the-completion-audit)).

<p align="center">
  <img src="snapshots/subagent.png" alt="Subagent tree in the desktop app" width="50%">
</p>

## Code intelligence (LSP)

Grepping for a symbol tells you where a name appears; a language server tells you what it *means*. A single read-only `lsp` tool exposes ten navigation operations — `goToDefinition`, `goToDeclaration`, `goToTypeDefinition`, `goToImplementation`, `findReferences`, `hover`, `documentSymbol`, `workspaceSymbol`, `callHierarchy`, `diagnostics` — backed by seven built-in server specs (Python/pyright, TypeScript, JavaScript, Go, Java, Rust, C/C++).

- **Nothing is bundled** — a server is used only if its binary is already on your `PATH`; otherwise the tool returns its install command (e.g. `npm i -g pyright`) instead of failing. Auto-install is **opt-in** via `auto_install` in `~/.cluxmate/lsp.json` or `<project>/.cluxmate/lsp.json` (deep-merged like `mcp.json`, with your own servers and argv), and installers never run in `plan` mode.
- **Diagnostics after every edit** — the four write tools ask the language server for **error-level** diagnostics and return them *inside the tool result*, so the model sees the breakage it just caused without spending a turn discovering it. An edit never installs a toolchain, warnings are dropped on purpose, and a server that stays silent can't stall a write.
- **Read-only by construction** — all ten operations query; none mutate. Install commands are your config, so the model can never cause a toolchain download by itself.

## Skills, memory & MCP

- **Skills** — project-scoped instruction packs (`<project>/.cluxmate/skills.json`) that the model can load on demand via the `use_skill` tool.
- **Memory** — a durable project memory file, `AGENTS.md`, rendered as a tagged synthetic message every turn. A legacy `CLAUDE.md` file is also honored as a read-only fallback.
- **Retrieval memory (opt-in)** — `remember` / `forget` tools store cross-session facts as individual Markdown files; the most relevant facts are auto-recalled into each turn via lexical FTS5 scoring. Facts are scoped **project** (default) or **global**; off by default (`~/.cluxmate/retrieval-memory.json`).
- **MCP** — Model Context Protocol servers (stdio or HTTP) plug their tools straight into the agent's context; servers are loaded once per working directory, and stdio servers are OS-sandboxed like `bash`. Remote servers that speak OAuth are covered [below](#remote-mcp-servers--oauth).

## Remote MCP servers & OAuth

Remote (HTTP) MCP servers can authenticate in three ways, resolved in this order:

1. **Explicit `oauth`** in `mcp.json` — wins over everything else:

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

   Every field is optional: without `client_id` the client registers dynamically; `client_secret_env` holds the *name* of an environment variable; `oauth: false` turns OAuth off explicitly; `oauth: true` enables it with defaults.
2. **A static `Authorization` header** (or `authorization_env`) — if you wrote credentials down yourself, they are used as written.
3. **Latent OAuth** — a remote server with no static header can still use a stored token if one exists; a `401` is what surfaces the login prompt (never a pre-emptive browser window).

- **Log in and out from either front-end** — the desktop MCP panel shows a `needs_auth` badge with a login button and an expiry time once connected; the CLI does the same headlessly:

  ```bash
  cluxmate mcp status                 # servers + auth state
  cluxmate mcp auth linear            # opens the browser, waits on a loopback callback
  cluxmate mcp logout linear          # delete stored credentials
  ```

- **Tokens stay out of reach** — credentials are written `0600` to `~/.cluxmate/mcp-auth.json`, always denied to the read tools, and a token bound to a URL is not reused if the endpoint changes. The model has **no tool** that can start an authorization; only your action can.
- **Refresh is automatic, failure is honest** — an expired token with a refresh token is refreshed silently; a rejected refresh deletes the credential (a transient failure keeps it). `needs_auth` (you have an action) and `failed` (read the error) are deliberately distinct states. At most one forced refresh-and-retry per call.
- **OAuth is for remote servers only** — `oauth` on a stdio server is a config error, not something silently ignored.


## Lifecycle hooks

Run your own shell commands at fixed points (Claude Code style). Commands receive context as stdin JSON and decide to block or inject via stdout JSON:

| Event | Fires | Can block | Can inject |
|---|---|---|---|
| `UserPromptSubmit` | before the prompt reaches the model | ✓ | ✓ |
| `PreToolUse` | before a tool runs (after approval) | ✓ (denies the tool) | ✓ |
| `PostToolUse` | after a tool runs | — | ✓ |
| `Stop` | after the reply, before it's committed | ✓ (re-runs the model, max 3) | ✓ |
| `SessionStart` | once per session start | ✓ (aborts startup) | ✓ (first turn only) |
| `SessionEnd` | clean shutdown / session switch / REPL `/clear` | — (output discarded) | — |
| `SubagentStop` | after a subagent finishes | ✓ (replaces its reply) | ✓ |
| `PreCompact` | before auto-compaction | ✓ (skips it this step) | ✓ |
| `Notification` | turn end (desktop) + `hooks/notify` RPC / "Test notify" button | — (fire-and-forget) | — |

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

- **Block**: output `{"decision":"block","reason":"..."}` (or `{"continue":false,...}`) or exit with code 2 → the action/reply is blocked and the model receives the reason.
- **Inject**: output `{"hookSpecificOutput":{"additionalContext":"..."}}` → extra context is injected for the model.
- **Location**: `~/.cluxmate/settings.json` (global) and `<project>/.cluxmate/settings.json` (project, runs after global) are merged. Event-specific payload fields (`source`, `reason`, `subagent_id`, `trigger`, `message`, …) ride along in the stdin JSON.
- **Trust model**: hooks are your own trusted config, running in a normal subprocess (not sandboxed); crashes/timeouts degrade to a no-op. Edits apply via Reload in the desktop Hooks view (or restart for CLI/TUI).

## Installation

### Requirements

- **Python ≥ 3.12**
- Node.js 18+ and npm (for the desktop app)

### 1. Python package

```bash
git clone https://github.com/r1c7/CluxMate.git   
cd cluxmate

pip install .            # direct install
# or, for development — editable install (your changes take effect immediately):
pip install -e .
pip install pytest pytest-asyncio   # dev deps are not in pyproject.toml
```

After installation the `cluxmate` command is available on your `PATH`.

### 2. Desktop app

The desktop app drives the Python agent via `cluxmate agent stdio`, so **install the Python package first** and make sure `cluxmate` is on your `PATH`.

```bash
cd desktop
npm install
```

Then run it in one of two ways:

- **Development mode** — launches the app with hot reload, for working on the desktop code:

  ```bash
  npm run dev
  ```

- **Build & package** — compiles and produces an installer for the current platform (`dist/`):

  ```bash
  npm run package          # current platform
  npm run package:win      # Windows installer
  npm run package:mac      # macOS DMG
  npm run package:linux    # Linux AppImage / deb
  ```

Other useful commands:

```bash
npm run build      # compile only (electron-vite build)
npm run preview    # preview the built app
npm run typecheck  # type-check main + renderer
```

## Usage

```bash
cluxmate -p "Explain the session log design"                    # headless one-shot
cluxmate -p "Refactor this" --model-id deepseek                 # with a specific model entry
cluxmate -p "..." --reasoning-effort high                       # reasoning level (per-dialect)
cluxmate repl                                                   # interactive REPL
cluxmate                                                        # Textual TUI
cluxmate agent stdio                                            # JSON-RPC stdio server (desktop backend)

cluxmate mcp status                                             # MCP servers + auth state
cluxmate mcp auth linear                                        # OAuth login for a remote MCP server
cluxmate mcp logout linear                                      # delete stored MCP credentials
```

Run `cluxmate` once to seed a default config, then pick a model in the TUI/desktop Settings.

## Configuration

Global config lives at `~/.cluxmate/config.json` (schema v2) — a list of model *entries* plus the active model:

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

- **`base_url`** — any OpenAI-compatible endpoint (DeepSeek, Qwen, GLM, OpenAI, OpenRouter, Ollama, self-hosted…).
- **`api_key`** — may be left empty; it falls back to the `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` environment variables.
- **`context_1m`** — enable for providers with 1M-token contexts.
- **`max_tokens`** — output budget; leave empty/`0` for the 32768 default.
- **`reasoning_efforts`** — optional per-model override of the reasoning-effort levels (the system ships presets per dialect: DeepSeek / Qwen / GLM / OpenAI).

Per-project state lives under `<project>/.cluxmate/` — it stays with the project, not your home directory:

| Scope | Files |
|---|---|
| **Project** (`<project>/.cluxmate/`) | `permissions.json` (always-allow lists), `mcp.json`, `lsp.json`, `settings.json` ([hooks](#lifecycle-hooks)), `skills/` (skill packs) + `skills.json` (which copies are disabled), `agents/` (custom subagent types), `memory/facts/` (retrieval facts), plus scratch dirs like `tmp-spill/` |
| **Global** (`~/.cluxmate/`) | `config.json` (models), `AGENTS.md` (global memory), `sandbox-grants.json` (extra writable folders), `forbid-read.json` (read denylist), `ssrf.json` (network allow/block), `egress.json` (bash/MCP egress mode), `retrieval-memory.json`, `mcp-auth.json` (OAuth tokens), `checkpoints/`, plus the global halves of the two-root families — `mcp.json`, `lsp.json`, `settings.json`, `agents/`, `skills/`, `memory/global/facts/` |

Config families with a global and a project half are resolved global-then-project: `mcp.json`, `lsp.json` and custom subagent types let the project copy override the global one, while hooks are **additive** — global hooks run first, then the project's. The registers the model must never be able to edit — writable-folder grants, the read denylist, the SSRF rules, the egress mode — are deliberately **user-global**, out of a prompt-injected model's reach.

## Desktop app

The Electron desktop is a full-featured front-end for the same Python core:

- **Sessions & working directories** — switch projects, resume or delete sessions, search past sessions.
- **Git integration** — current branch awareness, per-turn **checkpoint timeline**, diff viewer, and undo/checkout across branches.
- **Inspectors** — agent inspector, context viewer (what the model saw), subagent tree, tool-call cards, permission cards, and a live task-list panel driven by the agent's own `todo_write` calls.
- **Views** — hooks, MCP servers (with OAuth login / logout and a `needs_auth` badge), skills, settings (per-model configuration including reasoning effort, sandbox folder grants, the read denylist with its sensitive-file toggle, network access rules and egress mode, memory facts, theme, font, language, notification behaviour).
- **Stays in your way only when it must** — the taskbar icon flashes while a prompt is waiting on you (on macOS, a critical dock bounce).
- **Tray app** — stays in the system tray with a show/quit menu.

<p align="center">
  <img src="snapshots/codingwithcluxmate.png" alt="Coding with CluxMate in the desktop app" width="50%">
</p>


## License

[MIT](LICENSE) — see the [LICENSE](LICENSE) file for details.
