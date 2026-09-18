import { ChildProcess, spawn } from 'child_process'
import * as readline from 'readline'
import type { AgentTypeInfo, StreamEvent, ToolApproveResult } from '../shared/types'
import { agentEnv, pythonCommand } from './python-runtime'

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
  // Invoked when a background MCP authorization finishes (mcp/auth/completed).
  // The renderer refreshes its server list; the Python side has already
  // hot-swapped the client, so a re-fetch returns the NEW status (unlike
  // setMcpDisabled, whose effect is not visible until a new session).
  onMcpAuthCompleted: ((payload: { server: string; status: string; error?: string | null }) => void) | null = null
  // Invoked when the Python side asks about project trust (trust/required),
  // pushed right after an initialize that found withheld project config. The
  // renderer shows the prompt card; the answer comes back over trust/set.
  onTrustRequired?: (payload: { cwd: string; status: string; source: string; findings: unknown[]; store: Record<string, string> }) => void

  get isRunning(): boolean {
    return this._initialized && this.proc !== null && !this.proc.killed
  }

  async spawn(cwd: string, modelId: string, sessionId: string, trust?: 'trusted' | 'denied'): Promise<void> {
    await this.kill()
    this._spawnCwd = cwd

    return new Promise((resolve, reject) => {
      this.proc = spawn(pythonCommand(), ['-m', 'cluxmate', 'agent', 'stdio'], {
        cwd: cwd,
        env: agentEnv(),
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
          } else if (data.method === 'mcp/auth/completed') {
            this.onMcpAuthCompleted?.(data.params as { server: string; status: string; error?: string | null })
          } else if (data.method === 'trust/required') {
            this.onTrustRequired?.(data.params)
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
      const initParams: Record<string, unknown> = { cwd, model_id: modelId, session_id: sessionId, mode: this._mode }
      // A session-only decision ("trust this run only") dies with the Python
      // process, so the main process re-sends it on every spawn.
      if (trust) initParams.trust = trust
      this.request('initialize', initParams)
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

  // Public face of request() for the trust RPCs (ipc-handlers drives them on an
  // already-running bridge).
  async call(method: string, params: unknown): Promise<unknown> {
    return this.request(method, params)
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

  // Fold the persisted todo/write events into the session's current task list
  // (null = no list in force). Used by switchSession so the plan strip restores
  // after reopening a session — the display transcript doesn't carry it.
  async getSessionTodos(sessionId: string): Promise<{ todos: unknown[] | null }> {
    const r = (await this.request('session/todos', { session_id: sessionId })) as { todos?: unknown[] | null }
    return { todos: r?.todos ?? null }
  }

  // `sessionId` is this bridge's parent session (the process serving the RPC);
  // `targetSessionId` is whose log to reconstruct — a subagent's own <id>.jsonl
  // when inspecting a child, otherwise the parent. The bridge reads both from the
  // same shared SessionLogStore, so the RPC needs only the target id.
  async getTurnContexts(sessionId: string, targetSessionId?: string): Promise<{ turns: unknown[] }> {
    const r = (await this.request('session/context', { session_id: targetSessionId || sessionId })) as { turns?: unknown[] }
    return { turns: r?.turns ?? [] }
  }

  // Returns the engine's outcome rather than void: `always` reports whether the
  // rule is actually on disk (an untrusted project that the user did not consent
  // to writes nothing) and `trust` carries the fresh project-trust snapshot when
  // this click recorded one.
  async approveTool(callId: string, always = false, selected?: number[], trust = false): Promise<ToolApproveResult | null> {
    this._lastActivityAt = Date.now()
    return (await this.request('tool/approve', {
      call_id: callId, always, selected, trust,
    })) as ToolApproveResult | null
  }

  async denyTool(callId: string): Promise<void> {
    this._lastActivityAt = Date.now()
    await this.request('tool/deny', { call_id: callId })
  }

  async answerQuestion(callId: string, answers: { id: string; selected: string[]; custom?: string }[]): Promise<void> {
    this._lastActivityAt = Date.now()
    await this.request('question/answer', { call_id: callId, answers })
  }

  async getPermissions(): Promise<{ mode: string; accept_edits: boolean; always_allow_tools: string[]; always_allow_dangerous_tools: string[] }> {
    return (await this.request('permissions/get', {})) as {
      mode: string; accept_edits: boolean; always_allow_tools: string[]; always_allow_dangerous_tools: string[]
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

  async getAgents(): Promise<{ agents: AgentTypeInfo[]; errors: { path: string; error: string }[] }> {
    const r = (await this.request('agents/list', {})) as { agents?: AgentTypeInfo[]; errors?: { path: string; error: string }[] }
    return { agents: r?.agents ?? [], errors: r?.errors ?? [] }
  }

  async notifyHooks(message: string): Promise<{ status: string }> {
    this._lastActivityAt = Date.now()
    const r = (await this.request('hooks/notify', { message })) as { status?: string }
    return { status: r?.status ?? 'scheduled' }
  }

  async setMode(mode: string): Promise<{ mode: string; accept_edits: boolean; always_allow_tools: string[]; always_allow_dangerous_tools: string[] }> {
    this._mode = mode
    return (await this.request('chat/set_mode', { mode })) as {
      mode: string; accept_edits: boolean; always_allow_tools: string[]; always_allow_dangerous_tools: string[]
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

  // Start the interactive OAuth flow for one server. Resolves as soon as the
  // Python side has kicked off its background thread (status 'started'); the
  // real outcome arrives later as a mcp/auth/completed notification.
  async startMcpAuth(name: string): Promise<{ status: string; error?: string }> {
    return (await this.request('mcp/auth/start', { server: name })) as { status: string; error?: string }
  }

  async logoutMcp(name: string): Promise<{ status: string; removed?: boolean }> {
    return (await this.request('mcp/auth/logout', { server: name })) as { status: string; removed?: boolean }
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
    // A closure left over a dead window would leak; the next ensureBridge
    // re-assigns it for the new process.
    this.onMcpAuthCompleted = null
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
