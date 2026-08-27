import { ChildProcess, spawn } from 'child_process'
import * as readline from 'readline'
import { app } from 'electron'
import { delimiter, join } from 'path'
import { existsSync } from 'fs'
import type { StreamEvent } from '../shared/types'

// Prefer the cluxmate source bundled with the app (electron-builder.yml
// extraResources → <resources>/cluxmate/) over whatever the user may have
// pip-installed, so the desktop shell and the agent core are version-locked
// to the same release. Third-party deps (openai/jinja2/httpx) still come from
// the user's Python env — full self-containment is the PyInstaller step, not
// this one. Returns the PYTHONPATH entry to prepend, or null when not packaged
// (dev runs against the source checkout via `python -m`).
function bundledCluxmatePythonpath(): string | null {
  if (!app.isPackaged) return null
  const resources = process.resourcesPath
  if (existsSync(join(resources, 'cluxmate', '__init__.py'))) {
    return resources
  }
  // Defensive: allow the package to be nested one level deeper.
  if (existsSync(join(resources, 'cluxmate', 'cluxmate', '__init__.py'))) {
    return join(resources, 'cluxmate')
  }
  return null
}

export class AgentBridge {
  private proc: ChildProcess | null = null
  private rl: readline.Interface | null = null
  private requestId = 0
  private responseHandlers = new Map<number, (data: any) => void>()
  private streamHandlers = new Map<number, (event: StreamEvent) => void>()
  private lineHandler: ((line: string) => void) | null = null
  private _initialized = false
  _spawnCwd: string = ''
  // Development mode carried across the initialize handshake and reused on
  // respawn. Per-session, not persisted; defaults to 'default'.
  _mode: string = 'default'
  // Timestamp of the last user-facing activity (chat send / tool approval),
  // used by the idle reaper to kill long-idle processes. Epoch ms.
  _lastActivityAt: number = Date.now()
  // True while a chat/send is in flight — the reaper must never kill a bridge
  // mid-turn (the user may be waiting on a tool-approval prompt for minutes).
  _busy: boolean = false
  // Invoked when the process exits unexpectedly (crash / external kill). The
  // ipc-handlers layer wires this to notify the renderer so the sidebar dot
  // greys out in real time, matching the idle-reaper notification path.
  onExit: (() => void) | null = null

  get isRunning(): boolean {
    return this._initialized && this.proc !== null && !this.proc.killed
  }

  async spawn(cwd: string, modelId: string, sessionId: string): Promise<void> {
    await this.kill()
    this._spawnCwd = cwd

    return new Promise((resolve, reject) => {
      const pythonCmd = process.platform === 'win32' ? 'python' : 'python3'
      const env: NodeJS.ProcessEnv = {
        ...process.env,
        PYTHONIOENCODING: 'utf-8',
        PYTHONUTF8: '1',
        PYTHONUNBUFFERED: '1',
      }
      const bundled = bundledCluxmatePythonpath()
      if (bundled) {
        // Prepend the bundled core so it wins over any pip-installed cluxmate,
        // while still letting the user's env provide third-party deps.
        env.PYTHONPATH = env.PYTHONPATH
          ? `${bundled}${delimiter}${env.PYTHONPATH}`
          : bundled
      }
      this.proc = spawn(pythonCmd, ['-m', 'cluxmate', 'agent', 'stdio'], {
        cwd: cwd,
        env,
        stdio: ['pipe', 'pipe', 'pipe'],
      })

      this.rl = readline.createInterface({ input: this.proc.stdout! })

      this.lineHandler = (line: string) => {
        try {
          const data = JSON.parse(line)
          if (data.id !== undefined && this.responseHandlers.has(data.id)) {
            this.responseHandlers.get(data.id)!(data)
            this.responseHandlers.delete(data.id)
          } else if (data.method === 'chat/stream') {
            this.streamHandlers.forEach(h => h(data.params as StreamEvent))
          }
        } catch { /* skip parse errors */ }
      }

      this.rl.on('line', this.lineHandler)

      const proc = this.proc
      proc.on('error', reject)
      proc.on('close', (code) => {
        if (proc !== this.proc) return
        this._initialized = false
        if (this.responseHandlers.size > 0) {
          const msg = code === 0 ? 'Agent process exited' : `Agent process exited with code ${code}`
          this.responseHandlers.forEach(h => h({ error: { message: msg } }))
          this.responseHandlers.clear()
        }
        this.proc = null
        this.rl = null
        // Notify the renderer so the sidebar dot greys out immediately on an
        // unexpected exit (crash / external kill). Fires only for the CURRENT
        // proc (guard above), so a superseding spawn doesn't double-notify.
        this.onExit?.()
      })
      proc.stderr?.on('data', (data: Buffer) => {
        console.error('[python stderr]', data.toString())
      })

      // Initialize handshake. Pass the current mode so a respawn (e.g. after a
      // crash) restores it; on a fresh spawn it's the 'default' default.
      this.request('initialize', { cwd, model_id: modelId, session_id: sessionId, mode: this._mode })
        .then(() => { this._initialized = true; resolve() })
        .catch((e) => {
          this._initialized = false
          this.kill()
          reject(e)
        })
    })
  }

  private request(method: string, params: unknown, timeoutMs = 300000): Promise<unknown> {
    return new Promise((resolve, reject) => {
      if (!this.proc || this.proc.killed) {
        reject(new Error('Agent not running'))
        return
      }

      const id = ++this.requestId
      const msg = JSON.stringify({ jsonrpc: '2.0', id, method, params }) + '\n'

      this.responseHandlers.set(id, (data: any) => {
        if (data.error) {
          reject(new Error(data.error.message))
        } else {
          resolve(data.result)
        }
      })

      this.proc.stdin!.write(msg)

      setTimeout(() => {
        if (this.responseHandlers.has(id)) {
          this.responseHandlers.delete(id)
          reject(new Error(`Request ${method} timed out`))
        }
      }, timeoutMs)
    })
  }

  async streamChat(
    message: string,
    onEvent: (event: StreamEvent) => void,
    options?: { modelId?: string; reasoningEffort?: string | null }
  ): Promise<unknown> {
    if (!this.isRunning) {
      throw new Error('Agent not connected. Initialize may have failed.')
    }

    this._busy = true
    this._lastActivityAt = Date.now()

    // Clear any stale stream handlers from a prior cancelled chat
    this.streamHandlers.clear()

    const id = ++this.requestId
    const params: Record<string, unknown> = { message: [{ type: 'text', text: message }] }
    if (options?.modelId) params.model_id = options.modelId
    if (options?.reasoningEffort !== undefined) params.reasoning_effort = options.reasoningEffort
    const msg = JSON.stringify({
      jsonrpc: '2.0', id, method: 'chat/send',
      params,
    }) + '\n'

    return new Promise((resolve, reject) => {
      this.streamHandlers.set(id, onEvent)

      this.responseHandlers.set(id, (data: any) => {
        this.streamHandlers.delete(id)
        this._busy = false
        if (data.error) {
          reject(new Error(data.error.message))
        } else {
          resolve(data.result)
        }
      })

      this.proc!.stdin!.write(msg)

      // No wall-clock timeout: a chat can legitimately run for minutes while
      // it waits on the user's tool-approval decision (the Python side no
      // longer caps this). The promise still settles — it resolves on the
      // agent's response, and rejects via proc.on('close') if the process
      // dies. The user cancels an unwanted run with the Stop button.
    })
  }

  async cancel(): Promise<void> {
    await this.request('chat/cancel', {})
  }

  async truncateSession(sessionId: string, seq: number): Promise<void> {
    await this.request('session/truncate', { session_id: sessionId, seq })
  }

  async replaySession(sessionId: string): Promise<{ subagents: unknown[] }> {
    const r = (await this.request('session/replay', { session_id: sessionId })) as { subagents?: unknown[] }
    return { subagents: r?.subagents ?? [] }
  }

  // `sessionId` is this bridge's parent session (the process serving the RPC);
  // `targetSessionId` is whose log to reconstruct — a subagent's own <id>.jsonl
  // when inspecting a child, otherwise the parent. The bridge reads both from the
  // same shared SessionLogStore, so the RPC needs only the target id.
  async getTurnContexts(sessionId: string, targetSessionId?: string): Promise<{ turns: unknown[] }> {
    const r = (await this.request('session/context', { session_id: targetSessionId || sessionId })) as { turns?: unknown[] }
    return { turns: r?.turns ?? [] }
  }

  async approveTool(callId: string, always = false, selected?: number[]): Promise<void> {
    this._lastActivityAt = Date.now()
    await this.request('tool/approve', { call_id: callId, always, selected })
  }

  async denyTool(callId: string): Promise<void> {
    this._lastActivityAt = Date.now()
    await this.request('tool/deny', { call_id: callId })
  }

  async answerQuestion(callId: string, answers: { id: string; selected: string[]; custom?: string }[]): Promise<void> {
    this._lastActivityAt = Date.now()
    await this.request('question/answer', { call_id: callId, answers })
  }

  async getPermissions(): Promise<{ mode: string; accept_edits: boolean; always_allow_tools: string[] }> {
    return (await this.request('permissions/get', {})) as {
      mode: string; accept_edits: boolean; always_allow_tools: string[]
    }
  }

  async getHooks(): Promise<{ hooks: { event: string; matcher: string | null; command: string; timeout: number }[] }> {
    const r = (await this.request('hooks/get', {})) as { hooks?: { event: string; matcher: string | null; command: string; timeout: number }[] }
    return { hooks: r?.hooks ?? [] }
  }

  async reloadHooks(): Promise<{ hooks: { event: string; matcher: string | null; command: string; timeout: number }[] }> {
    const r = (await this.request('hooks/reload', {})) as { hooks?: { event: string; matcher: string | null; command: string; timeout: number }[] }
    return { hooks: r?.hooks ?? [] }
  }

  async setMode(mode: string): Promise<{ mode: string; accept_edits: boolean; always_allow_tools: string[] }> {
    this._mode = mode
    return (await this.request('chat/set_mode', { mode })) as {
      mode: string; accept_edits: boolean; always_allow_tools: string[]
    }
  }

  async listCheckpoints(): Promise<unknown> {
    const r = (await this.request('checkpoint/list', {})) as { checkpoints?: unknown }
    return r?.checkpoints ?? []
  }

  async diffCheckpoint(checkpointId: string): Promise<unknown> {
    const r = (await this.request('checkpoint/diff', { checkpoint_id: checkpointId })) as { files?: unknown }
    return r?.files ?? []
  }

  async restoreCheckpoint(checkpointId: string): Promise<unknown> {
    return await this.request('checkpoint/restore', { checkpoint_id: checkpointId })
  }

  async listMcp(): Promise<unknown> {
    const r = (await this.request('mcp/list', {})) as { servers?: unknown }
    return r?.servers ?? []
  }

  async kill(): Promise<void> {
    if (this.rl && this.lineHandler) {
      this.rl.off('line', this.lineHandler)
    }
    const oldProc = this.proc
    // Send mcp:shutdown BEFORE killing the Python process. Windows
    // TerminateProcess skips Python's atexit handlers, so without this RPC
    // any spawned MCP stdio subprocesses would be orphaned. Best-effort with
    // a 2s timeout — don't block the kill on a hung Python.
    if (oldProc && !oldProc.killed && this._initialized) {
      try {
        await this.request('mcp:shutdown', {}, 2000)
      } catch { /* best-effort — fall through to SIGTERM/SIGKILL */ }
    }
    this.proc = null
    this.rl = null
    this.responseHandlers.clear()
    this.streamHandlers.clear()
    if (oldProc && !oldProc.killed) {
      oldProc.kill('SIGTERM')
      setTimeout(() => {
        if (oldProc && !oldProc.killed) {
          oldProc.kill('SIGKILL')
        }
      }, 5000)
    }
  }
}
