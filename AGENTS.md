# Project memory

## What CluxMate is

- CluxMate = one Python agent core (`cluxmate/`) driving three front-ends: headless CLI (`cluxmate -p "..."`), Textual TUI (`cluxmate`), and an Electron desktop (`desktop/`) that drives the core over JSON-RPC stdio (`cluxmate agent stdio`). Providers are OpenAI-compatible only (DeepSeek/Qwen/OpenAI/any `base_url`); there is no Anthropic provider.
- Four risk modes (`plan` / `default` / `acceptEdits` / `yolo`): `plan` registers only read tools; `default` approves `safe` automatically, asks for `write`/`dangerous`; `acceptEdits` auto-approves `write`; `yolo` disarms the sandbox boundaries entirely (the one explicit opt-out).
- The repo root is NOT a git repository (no `.git/`) — `git status`/commits fail here; `snapshots/` holds only UI screenshots. Checkpoint shadow-git repos live in `~/.cluxmate/checkpoints/<sha1(cwd)>.git` (see `core/checkpoints.py`), independent of the user's repo.

## Architecture — core invariants

- The session log is **event-sourced and the source of truth** (`cluxmate/core/session_log.py` docstring): provider message history is *derived* from the append-only log, never stored separately. Model-visible request == `[system from fold_request_header] + derive_messages()`; `derive_messages()` deliberately excludes the system prompt. `request/header` events record the full non-history envelope (config + system prompt + tool schemas) with reason `initial`/`change`; only changed headers are appended. Compaction rewrites a *surface* projection via `replace` ops without erasing the underlying events, preserving a cache-stable prefix.
- Environment context — memory, skills, mode — is injected as **tagged synthetic `user/message` events** (`source: memory` / `skill` / `mode`), NOT baked into the system prompt, so request prefixes stay prompt-cache stable. A mode or tool-schema change appends a fresh `request/header` with reason `change`.
- `AgentBuilder` (fluent, `core/builder.py`) is the single wiring point: it registers tools, the WriteFence, the bash/MCP sandbox, and subagent types. A `chat/set_mode` change rebuilds the agent so a mode switch re-arms/disarms sandbox boundaries.
- Subagents are child `SessionLog`s linked to the parent via `subagent/spawn`; replay walks the whole delegation tree.
- Torn session tails are dropped on load; an interrupted turn is closed with synthetic `tool/result` + `turn/end {interrupted}`; undo rewinds to a turn boundary via a single `truncate`.

## Security: sandbox (READ the docs before touching this code)

Two enforcement boundaries sit behind the permissions policy (`core/permissions.py` decides what is *allowed*; the boundaries make denials stick). Authoritative Chinese docs: `docs/plans/sandbox-current-state.md` (current implementation) and `sandbox-threat-model.md` (decisions/threat model). Read them before touching `tools/_fence.py` / `tools/_sandbox.py` / `core/grants.py`.

- **WriteFence (T1, in-process)** — guards the 5 file tools `write_file` / `search_replace` / `multi_edit` / `multi_write` / `delete_file` (batch tools check each item; out-of-bounds items are skipped). Canonicalize-then-contain: `resolve(strict=False)` (resolves `..` and symlinks) → deny check → contain check, before any I/O.
  - Writable roots defined in exactly one place: `WriteFence.roots()` = session cwd + platform temp dir + exactly `~/.cluxmate/AGENTS.md` (whitelisted so `update_memory`'s "edit global entries with search_replace" contract works). The rest of `~/.cluxmate` (config.json with API keys, session logs, checkpoints) is off-limits.
  - `<cwd>/.cluxmate/` is a **deny subtree** (takes precedence over roots): it holds CluxMate's privileged project state (permissions.json always-allow, mcp.json which spawns subprocesses on load, skills.json) — a prompt-injected model must not edit its own permission config.
  - Enabled in all modes except `yolo`.
- **Bash + MCP stdio sandbox (T2, OS-level)** — glue over OS primitives, nothing from scratch: Linux = bubblewrap (bwrap) mount namespaces (root fs read-only, workspace+temp bind-mounted writable, network stays shared — documented omission); Windows = Low integrity-level token with `NO_WRITE_UP`, workspace tree labeled low-IL, `<cwd>/.cluxmate` deliberately re-labeled medium so the sandboxed shell can't edit permission config. Low-IL can still READ everything and reach the network — enforcement is partial by design.
  - **Fail-closed**: sandbox enabled + no backend available → BashTool refuses to run (never falls back to bare subprocess). Escape hatch: `CLUXMATE_BASH_SANDBOX=off` (explicit, env-scoped). Note: the sandbox backend is chosen once when tools are built — changing the env var mid-process does nothing.
- `core/grants.py` = the always-allow registry (sandbox-grants). `dangerous` commands always prompt; always-allow never applies to them.

## Hooks

Lifecycle hooks (Claude-Code-style): user-defined shell commands at `UserPromptSubmit` / `PreToolUse` / `PostToolUse` / `Stop`, receiving a JSON payload on stdin and writing JSON to stdout — `{"decision":"block","reason":...}` or exit code 2 blocks; `{"hookSpecificOutput":{"additionalContext":...}}` injects context; anything else continues. `Stop` gets the full reply as `response` and can block + re-run (max 3). Configured in `settings.json` (global `~/.cluxmate/` + project `<cwd>/.cluxmate/`, project runs after global). Trust model: hooks are the user's own config, NOT sandboxed, not model output. Authoritative doc: `docs/plans/hooks.md` (D1–D12). Read before touching `core/hooks.py` / `core/agent.py` (Stop block/retry) / `core/jsonrpc_server.py` (hooks/get + hook_start/hook_result/text_restart stream events).

## State & config layout

- Per-project state: `<cwd>/.cluxmate/` — `permissions.json` (always-allow), `mcp.json`, `skills.json`, `sandbox-grants`, `settings.json` (hooks), `tmp-low` (sandbox Low-IL temp). Stays with the project, not home.
- Global config: `~/.cluxmate/config.json` schema v2 — `{"version": 2, "models": [{id, api_type, provider, base_url, api_key, model_name, context_1m, max_tokens}], "active_model_id"}`. `api_key` may be empty (falls back to `DEEPSEEK_API_KEY` / `OPENAI_API_KEY` env vars). Global memory: `~/.cluxmate/AGENTS.md`.
- **Memory files** — `core/memory.py`: AGENTS.md is the only memory file (matches Codex convention). `CLAUDE.md` (`<cwd>/CLAUDE.md`, `~/.claude/CLAUDE.md`) is only a *read* fallback when AGENTS.md is absent (legacy Claude Code compat); writes always target AGENTS.md. Rendered global-first then project-second as a `source:"memory"` synthetic message each turn. Cap: 32 KiB per file.

## Layout

- `cluxmate/core/` — agent.py (AgentLoop), builder.py (wiring), session_log.py + session_log_store.py, session_store.py, context.py (token estimation), config.py, permissions.py, grants.py, hooks.py, checkpoints.py (shadow-git undo), mcp.py, memory.py, skills.py, reasoning.py, jsonrpc_server.py, providers/ (factory + base, OpenAI-compatible only).
- `cluxmate/tools/` — bash, file tools, grep, list_dir, read_file, web_fetch, web_search, ask_user_question, task (subagents), skill, update_memory + `_fence.py`, `_sandbox.py`, `_fileio.py`.
- `cluxmate/tui/` (Textual app), `cluxmate/templates/` (system prompt `.j2`).
- `tests/` — `core/` and `tools/` unit tests + `test_integration.py`.
- `docs/plans/` — Chinese design docs: sandbox-current-state.md, sandbox-threat-model.md, hooks.md. `docs/competitive-analysis.md` (2026-08-25) = source-based comparison of 11 products with feature matrix, gaps G1–G17, roadmap.
- `desktop/` — Electron + electron-vite; shells out to `cluxmate agent stdio` (Python package must be installed first).

## Commands

- `pip install -e .` → entry point `cluxmate`. Requires Python ≥ 3.12. Dev deps (`pytest`, `pytest-asyncio`) are NOT in pyproject.toml — install manually.
- Headless: `cluxmate -p "prompt" [--model-id <id>] [--reasoning-effort <id>]`; REPL: `cluxmate repl`; TUI: `cluxmate`; JSON-RPC stdio server: `cluxmate agent stdio` (also `agent serve`).
- `pytest` runs from repo root (see environment gotchas below — may be unrunnable in this shell).
- Desktop: `cd desktop && npm run dev | build | typecheck | package` (package = electron-vite build + electron-builder --win).
- `CLUXMATE_DEBUG_REQUESTS=1` dumps every outgoing LLM request (messages + tool schemas) to stderr.

## Environment gotchas (Windows, this machine)

- Shell is cmd.exe. **Bash commands execute as a Low-integrity subprocess** (the outer harness's sandbox): `whoami /groups` shows `Mandatory Label\Low Mandatory Level`. The file tools (write_file / search_replace / etc.) run in the main process at Medium and are unaffected.
- In the current harness state NO directory is writable from bash — the workspace, home, and temp dirs are Medium-labeled while the subprocess runs Low, so `tempfile.gettempdir()` raises and **pytest fails before collection** with "No usable temporary directory found". Running tests requires restarting the outer harness with `CLUXMATE_BASH_SANDBOX=off` (bash then runs as Medium) or re-applying the Low labels; changing the env var mid-session does nothing because the sandbox is chosen once when tools are built.
- `TMP` / `TEMP` / `TMPDIR` point to `<cwd>\.cluxmate\tmp-low` (the sandbox's Low-IL temp). `C:\Users\rui\AppData\Local\Temp` exists but is unwritable from the Low subprocess.
- Shell quirks: quoted absolute paths fail (`dir "C:\..."` → "The filename, directory name, or volume label syntax is incorrect") — use unquoted paths; `python -c "..."` gets mangled — write a `.py` file and run it instead.

## Reference corpus & competitive analysis

- Cross-project reference corpus: `E:\workspace\agents` (OpenAI Codex `codex/`, DeepSeek Harness `deepseek-harness/`, Reasonix `DeepSeek-Reasonix/`, Grok Build `grok-build/`, MiMo-Code `MiMo-Code/`, OpenCode `opencode2/`, pi `pi/`). The old `E:\workspace\cluxmate_research` path no longer exists. Claude Code / Cursor / Trae IDE are closed source (not downloaded).
- Competitor-sandbox correction (verified against DeepSeek-Reasonix source/docs): Reasonix DOES have OS-level sandboxing on macOS (Seatbelt, internal/sandbox/seatbelt_darwin.go) and Linux (bubblewrap, internal/sandbox/prepare_linux.go), with a `[sandbox] forbid_read` (hide sensitive paths like ~/.ssh) and `[sandbox] network` egress switch. BUT Reasonix has NO OS-level Bash sandbox on Windows — its native Windows sandbox was retired (releases.json "Windows native sandbox retired"; reasonix.example.toml "Windows fixes this to off"). This is why CluxMate's Windows Low-IL sandbox is a genuine differentiator: Codex (Windows DACL restricted token) and DeepSeek Harness (win-acl) are the only other head agents shipping a Windows OS sandbox; Reasonix/Grok/OpenCode focus macOS/Linux.
