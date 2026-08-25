import { ipcMain, BrowserWindow, shell, app, dialog, clipboard } from 'electron'
import { execFile } from 'child_process'
import * as fs from 'fs'
import * as path from 'path'
import { IPC } from '../shared/ipc-channels'
import type { CreateSessionParams, StreamEvent, ChatMessage, SkillMeta, McpServer, GroupMeta, GitCheckoutStrategy, SessionSearchHit, HookEntry } from '../shared/types'
import { deriveSessionTitle } from '../shared/session-title'
import { AgentBridge } from './agent-bridge'
import * as sessionStore from './session-store'
import * as gitService from './git-service'
import { version as appVersion } from '../../package.json'

const bridges = new Map<string, AgentBridge>()
const pendingSpawns = new Map<string, Promise<void>>()
let activeSessionId: string | null = null

// Full session teardown shared by SESSION_DELETE and GROUP_DELETE: DB row +
// on-disk logs first, then the bridge, then the active pointer.
function deleteSessionFully(id: string) {
  sessionStore.deleteSession(id)
  const b = bridges.get(id)
  if (b) { b.kill(); bridges.delete(id) }
  if (activeSessionId === id) activeSessionId = null
}

function getMainWindow(): BrowserWindow {
  const win = BrowserWindow.getAllWindows()[0]
  if (!win) throw new Error('No window')
  return win
}

interface ModelEntry {
  id: string
  api_type: string
  provider: string
  base_url: string
  api_key: string
  model_name: string
  context_1m: boolean
  max_tokens?: number
  reasoning_efforts?: string[]
}

function configPath(): string {
  return path.join(app.getPath('home'), '.cluxmate', 'config.json')
}

// Same working directory? Resolves symlinks + canonical case so two spellings of
// one path count as the same project. Falls back to plain string equality when
// the path can't be resolved (e.g. the directory no longer exists).
function sameCwd(a: string, b: string): boolean {
  if (!a || !b) return false
  try { return fs.realpathSync(a) === fs.realpathSync(b) } catch { return a === b }
}

// Project-scoped tool-approval policy, mirrors cluxmate/core/permissions.py.
// Lives at <cwd>/.cluxmate/permissions.json so "accept edits" is per-project
// and does not follow the user to a different working directory.
// mode is per-session (never persisted) — a cold bridge always reports
// 'default'. Only always_allow_tools lives on disk.
interface ProjectPermissions { mode: string; accept_edits: boolean; always_allow_tools: string[] }

function permissionsPath(cwd: string): string {
  return path.join(cwd, '.cluxmate', 'permissions.json')
}

function readProjectPermissions(cwd: string): ProjectPermissions {
  if (!cwd) return { mode: 'default', accept_edits: false, always_allow_tools: [] }
  try {
    const p = JSON.parse(fs.readFileSync(permissionsPath(cwd), 'utf-8'))
    return {
      mode: 'default',
      accept_edits: false,
      always_allow_tools: Array.isArray(p.always_allow_tools) ? p.always_allow_tools : [],
    }
  } catch {
    return { mode: 'default', accept_edits: false, always_allow_tools: [] }
  }
}

// Writable-folder grants (sandbox-grants.json) — user-global, mirrors
// cluxmate/core/grants.py + cluxmate/core/jsonrpc_server.py::sandbox/grants.
// The desktop reads/writes the file directly (no Python bridge needed) so the
// Settings UI works even with zero live sessions. On Windows the Low-IL label
// must be reconciled on revocation: removing a grant re-labels the folder
// Low → Medium (icacls) so low-IL children stop being able to write there.
function grantsPath(): string {
  return path.join(app.getPath('home'), '.cluxmate', 'sandbox-grants.json')
}

function readGrants(): string[] {
  try {
    const data = JSON.parse(fs.readFileSync(grantsPath(), 'utf-8'))
    return Array.isArray(data.paths) ? data.paths.filter((p: unknown) => typeof p === 'string') : []
  } catch {
    return []
  }
}

function writeGrants(paths: string[]): void {
  fs.mkdirSync(path.dirname(grantsPath()), { recursive: true })
  fs.writeFileSync(grantsPath(), JSON.stringify({ paths }, null, 2), 'utf-8')
}

// Restore a revoked grant Low → Medium (Windows only). Best-effort: failure is
// logged, not thrown — the JSON store is still authoritative for next launch.
function restoreGrantMedium(p: string): void {
  if (process.platform !== 'win32') return
  execFile(
    'icacls',
    [p, '/setintegritylevel', '(OI)(CI)M', '/T', '/C', '/Q'],
    (err) => {
      if (err) console.error(`[sandbox] restore Grant Medium failed for ${p}:`, err?.message)
    },
  )
}

// Convert the legacy {providers, default_provider} schema to v2 {models,
// active_model_id}. Mirrors ConfigManager._migrate in Python — ids are the old
// provider keys verbatim so both migrators produce identical output. Returns
// the migrated object (or the input unchanged when already v2 / empty).
function migrateConfig(config: any): { config: any; changed: boolean } {
  if (!config || typeof config !== 'object') return { config, changed: false }
  if (Array.isArray(config.models)) return { config, changed: false }
  const providers = config.providers
  if (!providers || typeof providers !== 'object') return { config, changed: false }

  const labels: Record<string, string> = { deepseek: 'DeepSeek', openai: 'OpenAI' }
  const models: ModelEntry[] = Object.entries(providers).map(([name, cfg]: [string, any]) => ({
    id: name,
    api_type: 'openai',
    provider: labels[name] || (name.charAt(0).toUpperCase() + name.slice(1)),
    base_url: cfg?.base_url || '',
    api_key: cfg?.api_key || '',
    model_name: cfg?.model || '',
    context_1m: false,
    max_tokens: 0,
  }))
  let active = config.default_provider || ''
  if (!models.some((m) => m.id === active)) active = models[0]?.id || ''
  return { config: { version: 2, models, active_model_id: active }, changed: true }
}

// Read config.json, migrating legacy schema in place (persisted so the next
// reader sees v2). Returns the model list + active id for the renderer.
function readModelsConfig(): { models: ModelEntry[]; activeId: string } {
  let config: any = {}
  try { config = JSON.parse(fs.readFileSync(configPath(), 'utf-8')) } catch { /* fresh */ }
  const { config: migrated, changed } = migrateConfig(config)
  if (changed) {
    try { fs.writeFileSync(configPath(), JSON.stringify(migrated, null, 2), 'utf-8') } catch { /* best effort */ }
    config = migrated
  } else {
    config = migrated
  }
  return { models: config.models || [], activeId: config.active_model_id || '' }
}

// The desktop-owned display transcript: the exact text + tool blocks the user
// saw, in order. Separate from the provider-native history file (which the
// Python agent owns and can't represent tool cards for the UI).
function displayPath(sessionId: string): string {
  return path.join(app.getPath('home'), '.cluxmate', 'sessions', `${sessionId}.display.json`)
}

function loadDisplay(sessionId: string): ChatMessage[] {
  try {
    const data = JSON.parse(fs.readFileSync(displayPath(sessionId), 'utf-8'))
    return Array.isArray(data.messages) ? data.messages : []
  } catch {
    return []
  }
}

function saveDisplay(sessionId: string, messages: ChatMessage[]) {
  const dir = path.join(app.getPath('home'), '.cluxmate', 'sessions')
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(
    displayPath(sessionId),
    JSON.stringify({ messages, updated_at: new Date().toISOString() }, null, 2),
    'utf-8'
  )
}

// ── session full-text search ─────────────────────────────────────────────
// Search runs in the main process over each session's display transcript on
// disk (the same JSON the renderer reads on switch) — no Python bridge needed.
// A hit on title/cwd/provider/model short-circuits without touching the file.

const SEARCH_MAX_SNIPPETS = 3
const SEARCH_SNIPPET_RADIUS = 40 // chars of context shown before/after the match

// Fold text to a case-insensitive comparable form. Keyed to the LOWERCASE query
// so CJK (no case) and latin both match via plain substring `includes`.
function _norm(t: string): string { return t.toLowerCase() }

// Extract short fragments around each occurrence of `q` (already lowercased) in
// `text`. Returns at most SEARCH_MAX_SNIPPETS, each ~2*RADIUS chars wide,
// centered on the first match per fragment so the keyword is visible.
function _snippetsFor(text: string, q: string): string[] {
  const lower = _norm(text)
  const out: string[] = []
  let from = 0
  while (out.length < SEARCH_MAX_SNIPPETS) {
    const idx = lower.indexOf(q, from)
    if (idx === -1) break
    const start = Math.max(0, idx - SEARCH_SNIPPET_RADIUS)
    const end = Math.min(text.length, idx + q.length + SEARCH_SNIPPET_RADIUS)
    const snip = (start > 0 ? '…' : '') + text.slice(start, end) + (end < text.length ? '…' : '')
    // Advance past this match so repeated keywords produce distinct snippets.
    from = idx + q.length
    // De-dupe (overlapping windows can yield the same fragment).
    if (!out.includes(snip)) out.push(snip)
  }
  return out
}

// Collect every searchable body field of a display message: the flat `content`,
// each ordered text block, and each subagent node's text blocks.
function _messageSearchTexts(m: ChatMessage): string[] {
  const texts: string[] = []
  if (typeof m.content === 'string') texts.push(m.content)
  for (const b of m.blocks || []) {
    if (b.type === 'text') texts.push(b.text)
    // Subagent blocks live on nodes, not on `m.blocks`, so nothing extra here.
  }
  for (const node of Object.values(m.subagents || {})) {
    for (const b of node.blocks || []) {
      if (b.type === 'text') texts.push(b.text)
    }
    if (typeof node.result === 'string') texts.push(node.result)
  }
  return texts
}

function _searchSessions(query: string): SessionSearchHit[] {
  const q = _norm((query || '').trim())
  if (!q) return []
  const hits: SessionSearchHit[] = []
  for (const s of sessionStore.listSessions()) {
    // 1) Metadata hit — visible in the row already, so no snippet needed.
    if (_norm(s.title).includes(q)
      || _norm(s.cwd).includes(q)
      || _norm(s.provider).includes(q)
      || _norm(s.model).includes(q)) {
      hits.push({ meta: { ...s, is_pinned: !!s.is_pinned }, snippets: [] })
      continue
    }
    // 2) Body hit — scan the display transcript on disk.
    const msgs = loadDisplay(s.id)
    const snippets: string[] = []
    for (const m of msgs) {
      for (const text of _messageSearchTexts(m)) {
        for (const snip of _snippetsFor(text, q)) {
          if (snippets.length < SEARCH_MAX_SNIPPETS && !snippets.includes(snip)) snippets.push(snip)
        }
        if (snippets.length >= SEARCH_MAX_SNIPPETS) break
      }
      if (snippets.length >= SEARCH_MAX_SNIPPETS) break
    }
    if (snippets.length > 0) hits.push({ meta: { ...s, is_pinned: !!s.is_pinned }, snippets })
  }
  return hits
}

// First user message text for a session, for titling — read from the display
// transcript (the provider history is now Python-owned JSONL).
function firstUserMessage(sessionId: string): string {
  const disp = loadDisplay(sessionId).find((m) => m.role === 'user')
  if (disp && typeof disp.content === 'string' && disp.content.trim()) return disp.content
  return ''
}

// --- skills discovery ------------------------------------------------------
// A skill is a directory containing a SKILL.md. We scan two roots: the global
// ~/.cluxmate/skills and the project's <cwd>/.cluxmate/skills. Each SKILL.md may
// begin with YAML frontmatter carrying name/description; we parse just those
// two keys (no YAML dep) and fall back to the directory name.

const SKILL_MAX_BYTES = 256 * 1024

function parseFrontmatter(md: string): { name?: string; description?: string } {
  // Frontmatter is a leading `---\n ... \n---` block. Only name/description
  // are read; values may be quoted. Anything else is ignored.
  if (!md.startsWith('---')) return {}
  const end = md.indexOf('\n---', 3)
  if (end === -1) return {}
  const block = md.slice(3, end)
  const out: { name?: string; description?: string } = {}
  for (const line of block.split('\n')) {
    const m = /^\s*(name|description)\s*:\s*(.*)$/.exec(line)
    if (m) {
      let v = m[2].trim()
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1)
      }
      out[m[1] as 'name' | 'description'] = v
    }
  }
  return out
}

function scanSkillsRoot(root: string, source: 'global' | 'project', disabledSlugs?: Set<string>): SkillMeta[] {
  const out: SkillMeta[] = []
  let entries: fs.Dirent[]
  try {
    entries = fs.readdirSync(root, { withFileTypes: true })
  } catch {
    return out // root doesn't exist — fine, just no skills there
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue
    const skillMd = path.join(root, e.name, 'SKILL.md')
    if (!fs.existsSync(skillMd)) continue
    let fm: { name?: string; description?: string } = {}
    try {
      fm = parseFrontmatter(fs.readFileSync(skillMd, 'utf-8').slice(0, 4096))
    } catch { /* unreadable — still list it by dir name */ }
    out.push({
      name: fm.name || e.name,
      description: fm.description || '',
      source,
      path: skillMd,
      disabled: disabledSlugs?.has(e.name) ?? false,
    })
  }
  return out
}

function skillRoots(cwd: string): { root: string; source: 'global' | 'project' }[] {
  return [
    { root: path.join(app.getPath('home'), '.cluxmate', 'skills'), source: 'global' },
    { root: path.join(cwd, '.cluxmate', 'skills'), source: 'project' },
  ]
}

// Read <cwd>/.cluxmate/skills.json and return the set of disabled skill slugs.
// Project-only (global disabled state isn't persisted — it only makes sense
// within the context of a project's tools).
function readSkillsDisabled(cwd: string): Set<string> {
  const cfgPath = path.join(cwd, '.cluxmate', 'skills.json')
  try {
    const raw = fs.readFileSync(cfgPath, 'utf-8')
    const cfg = JSON.parse(raw)
    if (cfg && cfg.disabledSkills && Array.isArray(cfg.disabledSkills)) {
      return new Set(cfg.disabledSkills.filter((s: unknown) => typeof s === 'string'))
    }
  } catch {}
  return new Set()
}

// A path is a legitimate skill file only if it's a SKILL.md directly inside a
// subdirectory of one of the known roots. Guards the read handler against
// path-traversal (e.g. a crafted "../../secret").
function isAllowedSkillPath(p: string, cwd: string): boolean {
  const resolved = path.resolve(p)
  if (path.basename(resolved) !== 'SKILL.md') return false
  const parent = path.dirname(path.dirname(resolved)) // <root>/<skill>/SKILL.md -> <root>
  return skillRoots(cwd).some((r) => path.resolve(r.root) === parent)
}

// Resolve which config entry id to spawn a session's bridge with. Prefer the
// session's own pinned model (a per-session selection now survives restart);
// rows created before the model_id schema (null) fall back to the global active
// model from config.json.
function resolveModelId(modelId: string | null | undefined): string {
  if (modelId) return modelId
  try { return readModelsConfig().activeId } catch { return '' }
}

async function ensureBridge(sid: string, cwd: string, modelId: string): Promise<AgentBridge> {
  // sid is passed to the Python side as session_id so checkpoints are tagged
  // and filtered per session.
  let b = bridges.get(sid)
  // If cwd changed, kill old bridge so we spawn a fresh one.
  if (b && b.isRunning && b._spawnCwd !== cwd) {
    bridges.delete(sid)
    b.kill().catch(() => {})
    b = undefined
  }
  if (b && b.isRunning) return b

  // CRITICAL: a spawn already in flight must be AWAITED, not killed. Bridges
  // set by a concurrent ensureBridge are isRunning=false until their
  // initialize handshake resolves (~8s). The old code fell straight through to
  // `if (b) b.kill()` below and SIGTERM'd that in-flight python — every rapid
  // MCP_LIST call (useEffect fires several) killed the previous one's process,
  // an infinite self-kill loop where initialize never resolves and no python
  // ever survives. Check pendingSpawns FIRST and wait on it.
  const pending = pendingSpawns.get(sid)
  if (pending) {
    try { await pending } catch { /* fall through and retry a fresh spawn */ }
    const b2 = bridges.get(sid)
    if (b2 && b2.isRunning) return b2
    // in-flight spawn failed; fall through to start a clean one
    b = bridges.get(sid)
  }

  // Remove a stale (non-running, not-in-flight) bridge before spawning.
  if (b) { b.kill(); bridges.delete(sid) }

  b = new AgentBridge()
  // Notify the renderer when this process exits unexpectedly (crash / external
  // kill) so the sidebar dot greys out in real time. Intentional kills (delete,
  // cwd change, idle reaper) already refresh the renderer through their own
  // paths, so the redundant close notification is harmless — the dot is already
  // correct or about to be respawned.
  b.onExit = () => {
    if (!bridges.has(sid)) return  // already removed by delete/reaper — they notify
    bridges.delete(sid)
    for (const win of BrowserWindow.getAllWindows()) {
      win.webContents.send(IPC.BRIDGE_STATUS_CHANGED, { sessionIds: [sid], running: false })
    }
  }
  bridges.set(sid, b)
  const spawnPromise = b.spawn(cwd, modelId, sid).catch((e) => {
    console.error(`Agent spawn failed for ${sid} at ${cwd}:`, e?.message)
    bridges.delete(sid)
  })
  pendingSpawns.set(sid, spawnPromise)
  try {
    await spawnPromise
  } finally {
    pendingSpawns.delete(sid)
  }
  // Announce "came online" so the sidebar dot lights up as soon as the
  // initialize handshake resolves. switchSession/create pre-warm the bridge in
  // the background and call refreshBridgeStatuses immediately — before this
  // ~1.7s handshake finishes — so without this push the dot would stay grey
  // until the user sent a message. Guard on isRunning: a failed spawn was
  // already deleted above and must not falsely light the dot.
  if (b.isRunning) {
    for (const win of BrowserWindow.getAllWindows()) {
      win.webContents.send(IPC.BRIDGE_STATUS_CHANGED, { sessionIds: [sid], running: true })
    }
  }
  return b
}

export function registerIpcHandlers() {
  startIdleReaper()

  // Window controls for the custom (frameless) title bar. The window is looked
  // up at call time so handlers register fine before createWindow() runs.
  ipcMain.handle(IPC.WINDOW_MINIMIZE, () => { getMainWindow().minimize() })
  ipcMain.handle(IPC.WINDOW_MAXIMIZE_TOGGLE, () => {
    const win = getMainWindow()
    if (win.isMaximized()) win.unmaximize()
    else win.maximize()
  })
  ipcMain.handle(IPC.WINDOW_CLOSE, () => { getMainWindow().close() })
  ipcMain.handle(IPC.WINDOW_IS_MAXIMIZED, () => getMainWindow().isMaximized())

  ipcMain.handle(IPC.SESSION_LIST, () => {
    const sessions = sessionStore.listSessions()
    // SQLite stores is_pinned as INTEGER (0/1); normalize to boolean for the
    // renderer so React short-circuit rendering and comparisons work correctly.
    const normalized = sessions.map((s) => ({ ...s, is_pinned: !!s.is_pinned }))
    // Backfill titles for older default-named sessions from their first user
    // message, so pre-existing history also shows a meaningful name.
    for (const s of normalized) {
      if (s.title && s.title !== 'New Session') continue
      const first = firstUserMessage(s.id)
      if (first) {
        s.title = deriveSessionTitle(first)
        sessionStore.updateSession(s.id, { title: s.title })
      }
    }
    return normalized
  })

  ipcMain.handle(IPC.SESSION_SEARCH, (_e, query: string) => {
    return _searchSessions(query)
  })

  ipcMain.handle(IPC.SESSION_CREATE, async (_, params: CreateSessionParams) => {
    // Reuse an existing empty session (never sent a message) instead of piling
    // up new "New Session" entries — but only one per PROJECT (cwd): each
    // directory keeps at most one empty session, while other projects may each
    // have their own. message_count is maintained on every chat/send, so === 0
    // is the "never used" signal here — EXCEPT that it's only written when the
    // turn finishes, so a session streaming its first message still reads 0.
    // Exclude any bridge that's mid-turn (_busy), or a "New Session" click
    // during that first stream would silently "reuse" the very session that's
    // generating, doing nothing visible.
    const existing = sessionStore.listSessions().find(
      (s) => s.message_count === 0 && !bridges.get(s.id)?._busy && sameCwd(s.cwd, params.cwd)
    )
    const meta = existing || sessionStore.createSession(params)
    activeSessionId = meta.id
    // Don't block the IPC response on the Python spawn+initialize handshake
    // (~8s): the renderer only needs `meta` to show the session, and the bridge
    // is pre-warmed in the background.
    void ensureBridge(meta.id, meta.cwd, resolveModelId(meta.model_id)).catch((e) => {
      console.error(`Pre-warm bridge failed for ${meta.id}:`, e?.message)
    })
    return { ...meta, is_pinned: !!meta.is_pinned }
  })

  ipcMain.handle(IPC.SESSION_DELETE, async (_, id: string) => {
    deleteSessionFully(id)
  })

  ipcMain.handle(IPC.SESSION_UPDATE_CWD, async (_, id: string, cwd: string) => {
    sessionStore.updateSessionCwd(id, cwd)
    // Don't kill bridge here — ensureBridge will detect the cwd
    // change and respawn when CHAT_SEND fires next.
  })

  // Persist the session's selected model + reasoning level (the composer's
  // per-session selection). Also re-derives the denormalized provider/model/
  // api_type so the sidebar label stays correct without a full session reload.
  ipcMain.handle(IPC.SESSION_SET_MODEL, async (_, id: string, modelId: string, reasoningEffort: string | null) => {
    const { models } = readModelsConfig()
    const entry = models.find((m) => m.id === modelId)
    if (!entry) throw new Error('Model not found')
    sessionStore.updateSession(id, {
      model_id: entry.id,
      api_type: entry.api_type,
      model: entry.model_name || '',
      provider: entry.provider || '',
      reasoning_effort: reasoningEffort ?? null,
    })
  })

  ipcMain.handle(IPC.SESSION_SWITCH, async (_, id: string) => {
    const meta = sessionStore.getSession(id)
    if (!meta) return null
    activeSessionId = id
    // Return the transcript immediately from disk. Do NOT block on spawning
    // the Python agent — that handshake is slow and can hang under rapid
    // session switching, which would freeze the whole session view. The
    // bridge is pre-warmed in the background; CHAT_SEND spawns it on demand
    // anyway if it isn't ready yet.
    void ensureBridge(id, meta.cwd, resolveModelId(meta.model_id)).catch((e) => {
      console.error(`Pre-warm bridge failed for ${id}:`, e?.message)
    })
    return { history: [], display: loadDisplay(id) }
  })

  ipcMain.handle(IPC.SESSION_SAVE_DISPLAY, async (_, id: string, messages: ChatMessage[]) => {
    saveDisplay(id, messages)
  })

  // Truncate the Python-owned JSONL log to `seq` (the undo anchor emitted in the
  // turn_start event). The bridge forwards to the session/truncate RPC.
  ipcMain.handle(IPC.SESSION_TRUNCATE, async (_, id: string, seq: number) => {
    const bridge = bridges.get(id)
    if (bridge) await bridge.truncateSession(id, seq)
  })

  // Reconstruct the subagent tree from the Python-owned JSONL (the authoritative
  // source). Used by switchSession to fill a tree the display transcript is
  // missing (e.g. a session first run in the TUI, or a lost display transcript).
  ipcMain.handle(IPC.SESSION_REPLAY, async (_, sid: string) => {
    const meta = sessionStore.getSession(sid)
    if (!meta) return { subagents: [] }
    let bridge = bridges.get(sid)
    if (!bridge || !bridge.isRunning) {
      bridge = await ensureBridge(sid, meta.cwd, resolveModelId(meta.model_id))
    }
    if (!bridge || !bridge.isRunning) return { subagents: [] }
    return await bridge.replaySession(sid)
  })

  // Reconstruct every turn's exact first-request context from the Python-owned
  // JSONL (the authoritative source). `sid` is the parent session (its bridge
  // serves the RPC and owns the shared log store); `targetSid` is whose log to
  // reconstruct — a subagent id when inspecting a child, otherwise the parent.
  // Falls back to an empty list while the bridge is still warming up.
  ipcMain.handle(IPC.SESSION_CONTEXT, async (_, sid: string, targetSid?: string) => {
    const meta = sessionStore.getSession(sid)
    if (!meta) return { turns: [] }
    let bridge = bridges.get(sid)
    if (!bridge || !bridge.isRunning) {
      bridge = await ensureBridge(sid, meta.cwd, resolveModelId(meta.model_id))
    }
    if (!bridge || !bridge.isRunning) return { turns: [] }
    return await bridge.getTurnContexts(sid, targetSid)
  })

  ipcMain.handle(IPC.SESSION_RENAME, (_, id: string, title: string) => {
    sessionStore.renameSession(id, title)
  })

  ipcMain.handle(IPC.SESSION_PIN, async (_, id: string, pinned: boolean) => {
    sessionStore.pinSession(id, pinned)
  })

  ipcMain.handle(IPC.BRIDGE_STATUS, async (_, ids: string[]) => {
    return ids.map((sid) => ({ sessionId: sid, running: bridges.get(sid)?.isRunning ?? false }))
  })

  ipcMain.handle(IPC.GROUP_LIST, () => {
    const groups = sessionStore.listGroups()
    return groups.map((g) => ({ ...g, is_auto: !!g.is_auto }))
  })

  ipcMain.handle(IPC.GROUP_CREATE, (_, name: string) => {
    return sessionStore.createGroup(name)
  })

  ipcMain.handle(IPC.GROUP_RENAME, (_, id: string, name: string) => {
    const g = sessionStore.getGroup(id)
    if (g?.is_auto) throw new Error('Cannot rename auto-created groups')
    sessionStore.renameGroup(id, name)
  })

  ipcMain.handle(IPC.GROUP_DELETE, (_, id: string) => {
    // Deleting a group/project deletes ALL sessions inside it (their bridges,
    // DB rows, and on-disk .jsonl logs), then the group row itself. Auto
    // groups are also removed by the last session's cleanup, but the explicit
    // delete is harmless and covers user groups.
    const groupSessions = sessionStore.listSessions().filter((s) => s.group_id === id)
    for (const s of groupSessions) deleteSessionFully(s.id)
    sessionStore.deleteGroup(id)
  })

  ipcMain.handle(IPC.GROUP_MOVE_SESSION, (_, sessionId: string, groupId: string | null) => {
    sessionStore.moveSessionToGroup(sessionId, groupId)
  })

  ipcMain.handle(IPC.GROUP_MOVE_SESSION_TO_PROJECT, (_, sessionId: string) => {
    sessionStore.moveSessionToProject(sessionId)
  })

  ipcMain.handle(IPC.CHAT_SEND, async (_, sid: string, text: string, options?: { modelId?: string; reasoningEffort?: string | null }) => {
    const meta = sessionStore.getSession(sid)
    if (!meta) throw new Error('Session not found')

    // Name the session after the user's first message (default title only).
    if (!meta.title || meta.title === 'New Session') {
      sessionStore.updateSession(sid, { title: deriveSessionTitle(text) })
    }

    // Stamp the session with the model it's actually about to use — the
    // composer's per-session selection when present, else the global active
    // model — so the sidebar label reflects the real request.
    const allModels = readModelsConfig()
    const modelId = options?.modelId || allModels.activeId
    const entry = allModels.models.find((m) => m.id === modelId)
    if (entry) {
      sessionStore.updateSession(sid, {
        model_id: entry.id,
        api_type: entry.api_type,
        model: entry.model_name || '',
        provider: entry.provider || '',
        reasoning_effort: options?.reasoningEffort !== undefined ? options.reasoningEffort : null,
      })
    }

    const bridge = await ensureBridge(sid, meta.cwd, resolveModelId(modelId))
    if (!bridge.isRunning) {
      throw new Error('Agent not connected. Please create a new session.')
    }

    const win = getMainWindow()

    const result = await bridge.streamChat(text, (event: StreamEvent) => {
      // Persist an LLM-suggested title, but never clobber one the user set: only
      // overwrite while it's still an auto-derived default (the 'New Session'
      // placeholder or the first-line title we set from `text` below/above).
      if (event.type === 'title_suggested' && event.title) {
        const cur = sessionStore.getSession(sid)
        const t = cur?.title
        if (!t || t === 'New Session' || t === deriveSessionTitle(text)) {
          sessionStore.updateSession(sid, { title: event.title })
        }
      }
      win.webContents.send(IPC.STREAM_EVENT, { sessionId: sid, ...event })
    }, options)

    if (result) {
      const r = result as any
      // The Python agent owns the JSONL log now; only refresh the desktop's
      // message_count. Skip on cancel/timeout (history is null).
      if (r.history != null) {
        sessionStore.updateSession(sid, { message_count: r.history.length })
      }
    }

    return result
  })

  ipcMain.handle(IPC.CHAT_CANCEL, async (_, sid: string) => {
    const bridge = bridges.get(sid)
    if (bridge) await bridge.cancel()
  })

  ipcMain.handle(IPC.TOOL_APPROVE, async (_, sid: string, callId: string, always?: boolean, selected?: number[]) => {
    const bridge = bridges.get(sid)
    if (bridge) await bridge.approveTool(callId, always ?? false, selected)
  })

  ipcMain.handle(IPC.TOOL_DENY, async (_, sid: string, callId: string) => {
    const bridge = bridges.get(sid)
    if (bridge) await bridge.denyTool(callId)
  })

  ipcMain.handle(IPC.TOOL_ANSWER_QUESTION, async (_, sid: string, callId: string, answers: { id: string; selected: string[]; custom?: string }[]) => {
    const bridge = bridges.get(sid)
    if (bridge) await bridge.answerQuestion(callId, answers)
  })

  ipcMain.handle(IPC.PERMISSIONS_GET, async (_, sid: string) => {
    const bridge = bridges.get(sid)
    if (bridge && bridge.isRunning) return await bridge.getPermissions()
    // Bridge cold: read the project's permissions.json directly so the UI still
    // reflects state right after a session switch, before any chat/send.
    const meta = sessionStore.getSession(sid)
    return readProjectPermissions(meta?.cwd || '')
  })

  // Lifecycle hooks (settings.json, global + project merged). The merged view is
  // owned by the per-session Python process (which read settings.json at
  // initialize) — unlike permissions there's no main-process file fallback, so a
  // cold bridge returns an empty list (the caller treats it as "warming up").
  ipcMain.handle(IPC.HOOKS_GET, async (_, sid: string): Promise<{ hooks: HookEntry[] }> => {
    const bridge = bridges.get(sid)
    if (bridge && bridge.isRunning) return await bridge.getHooks()
    return { hooks: [] }
  })

  // Re-read settings.json in place (no session restart) and return the new list.
  // A cold bridge is warmed first (the fresh spawn already reads settings.json at
  // initialize), so Reload never fails just because the session was just switched
  // to; it only errors on a missing session record or a genuine spawn failure.
  ipcMain.handle(IPC.HOOKS_RELOAD, async (_, sid: string): Promise<{ hooks: HookEntry[] }> => {
    const meta = sessionStore.getSession(sid)
    if (!meta) {
      throw new Error(`Session record not found (sid=${sid})`)
    }
    let bridge = bridges.get(sid)
    if (!bridge || !bridge.isRunning) {
      bridge = await ensureBridge(sid, meta.cwd, resolveModelId(meta.model_id))
    }
    if (!bridge || !bridge.isRunning) {
      throw new Error('Agent process failed to start (Python agent not ready)')
    }
    return await bridge.reloadHooks()
  })

  // Open a hooks settings.json in the user's editor. `global` → ~/.cluxmate/
  // settings.json; `project` → <session cwd>/.cluxmate/settings.json. Auto-creates
  // the file (empty {"hooks":{}} skeleton) when missing so the editor always lands
  // on a real, editable file. shell.openPath returns an error string on failure
  // (e.g. no default editor) — surface it rather than swallowing.
  ipcMain.handle(IPC.HOOKS_OPEN, async (_, sid: string, scope: 'global' | 'project'): Promise<void> => {
    let dir: string
    if (scope === 'global') {
      dir = path.join(app.getPath('home'), '.cluxmate')
    } else {
      const meta = sessionStore.getSession(sid)
      if (!meta) throw new Error('Session not found')
      dir = path.join(meta.cwd, '.cluxmate')
    }
    const filePath = path.join(dir, 'settings.json')
    if (!fs.existsSync(filePath)) {
      fs.mkdirSync(dir, { recursive: true })
      fs.writeFileSync(filePath, JSON.stringify({ hooks: {} }, null, 2) + '\n', 'utf-8')
    }
    const err = await shell.openPath(filePath)
    if (err) throw new Error(err)
  })

  ipcMain.handle(IPC.SANDBOX_GRANTS_GET, () => {
    return { paths: readGrants() }
  })

  ipcMain.handle(IPC.SANDBOX_GRANTS_SET, (_, paths: string[]) => {
    const next = Array.isArray(paths) ? paths.filter((p): p is string => typeof p === 'string' && p.trim() !== '') : []
    const prev = readGrants()
    const removed = prev.filter((p) => !next.includes(p))
    // Reconcile revocations FIRST: restore each removed folder Low → Medium so
    // a low-IL child can no longer write it, regardless of the JSON write.
    for (const p of removed) restoreGrantMedium(p)
    writeGrants(next)
    // Kill the active session's bridge so the next chat re-initializes the
    // Python agent with the new grant set (mirrors SAVE_MODELS_CONFIG).
    if (activeSessionId) {
      const b = bridges.get(activeSessionId)
      if (b) {
        bridges.delete(activeSessionId)
        b.kill().catch(() => {})
      }
    }
    return { paths: next, restored: removed }
  })

  ipcMain.handle(IPC.CHAT_SET_MODE, async (_, sid: string, mode: string) => {
    // Development mode is per-session and NOT persisted, so there's nothing to
    // write to disk. It must reach a live bridge to take effect (plan changes
    // the Python-side toolset). If the bridge is cold, ensure it — a mode switch
    // is an explicit user action worth warming the process for.
    const meta = sessionStore.getSession(sid)
    if (!meta) throw new Error('Session not found')
    const bridge = await ensureBridge(sid, meta.cwd, resolveModelId(meta.model_id))
    if (!bridge.isRunning) throw new Error('Agent not connected.')
    return await bridge.setMode(mode)
  })

  ipcMain.handle(IPC.CHECKPOINT_LIST, async (_, sid: string) => {
    const bridge = bridges.get(sid)
    if (!bridge || !bridge.isRunning) return []
    return await bridge.listCheckpoints()
  })

  ipcMain.handle(IPC.CHECKPOINT_DIFF, async (_, sid: string, checkpointId: string) => {
    const bridge = bridges.get(sid)
    if (!bridge || !bridge.isRunning) return []
    return await bridge.diffCheckpoint(checkpointId)
  })

  ipcMain.handle(IPC.CHECKPOINT_RESTORE, async (_, sid: string, checkpointId: string) => {
    // Undo can fire on a historical message right after a session switch, when
    // no message has been sent yet and the bridge may be cold. Ensure it's up
    // (the shadow repo is per-cwd, so restore works regardless of live state).
    const meta = sessionStore.getSession(sid)
    let bridge = bridges.get(sid)
    if ((!bridge || !bridge.isRunning) && meta) {
      bridge = await ensureBridge(sid, meta.cwd, resolveModelId(meta.model_id))
    }
    if (!bridge || !bridge.isRunning) return { restored: [], deleted: [] }
    return await bridge.restoreCheckpoint(checkpointId)
  })

  ipcMain.handle(IPC.CLIPBOARD_WRITE, (_, text: string) => {
    // Main-process clipboard is reliable regardless of load origin; the
    // renderer's navigator.clipboard is flaky under file:// (non-secure ctx).
    clipboard.writeText(typeof text === 'string' ? text : String(text))
  })

  ipcMain.handle(IPC.SKILL_LIST, (_, cwd: string): SkillMeta[] => {
    const base = cwd || process.cwd()
    const disabled = readSkillsDisabled(base)
    const all = skillRoots(base).flatMap((r) => scanSkillsRoot(r.root, r.source, disabled))
    // Stable order: global first, then project, alphabetical within each.
    return all.sort((a, b) =>
      a.source === b.source ? a.name.localeCompare(b.name) : a.source === 'global' ? -1 : 1
    )
  })

  ipcMain.handle(IPC.SKILL_SET_DISABLED, (_, cwd: string, slug: string, disabled: boolean): void => {
    if (!/^[A-Za-z0-9_-]+$/.test(slug)) {
      throw new Error('Invalid skill slug')
    }
    const cfgPath = path.join(cwd, '.cluxmate', 'skills.json')
    let cfg: any = {}
    try { cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8')) } catch {}
    let list: string[] = Array.isArray(cfg.disabledSkills) ? [...cfg.disabledSkills] : []
    if (disabled) {
      if (!list.includes(slug)) list.push(slug)
    } else {
      list = list.filter((s: string) => s !== slug)
    }
    cfg.disabledSkills = list
    if (list.length === 0) {
      delete cfg.disabledSkills
    }
    fs.mkdirSync(path.dirname(cfgPath), { recursive: true })
    fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2), 'utf-8')
  })

  ipcMain.handle(IPC.SKILL_READ, (_, filePath: string): string => {
    // Only serve SKILL.md files under a known skills root (both global and the
    // active session's project root count as known).
    const cwds = Array.from(bridges.values()).map((b) => b._spawnCwd).filter(Boolean)
    const candidates = cwds.length > 0 ? cwds : [process.cwd()]
    const ok = candidates.some((c) => isAllowedSkillPath(filePath, c))
    if (!ok) return 'Error: not an allowed skill path.'
    try {
      const buf = fs.readFileSync(filePath, 'utf-8')
      return buf.length > SKILL_MAX_BYTES
        ? buf.slice(0, SKILL_MAX_BYTES) + '\n\n[truncated]'
        : buf
    } catch (e: any) {
      return `Error reading skill: ${e?.message}`
    }
  })

  // Read a workspace file for the inline edit-diff preview. The path must
  // resolve inside the session's cwd (the agent only edits within its
  // workspace) — this both scopes the read and blocks path-traversal. Returns
  // null when the file is missing (e.g. a freshly created-then-deleted file) so
  // the caller can fall back gracefully.
  const FILE_READ_MAX_BYTES = 512 * 1024
  ipcMain.handle(IPC.FILE_READ, (_, sid: string, filePath: string): string | null => {
    const meta = sessionStore.getSession(sid)
    const cwd = meta?.cwd || process.cwd()
    const resolved = path.resolve(cwd, filePath)
    const rel = path.relative(cwd, resolved)
    if (rel.startsWith('..') || path.isAbsolute(rel)) {
      throw new Error('Refused to read outside the session workspace')
    }
    try {
      const buf = fs.readFileSync(resolved, 'utf-8')
      return buf.length > FILE_READ_MAX_BYTES ? buf.slice(0, FILE_READ_MAX_BYTES) : buf
    } catch {
      return null
    }
  })

  // Mirror CHECKPOINT_RESTORE's lazy-ensure pattern: the renderer may open the
  // MCP browser right after switching to a cold session, before any chat/send.
  // We need the Python process up so it can read mcp.json and report status.
  ipcMain.handle(IPC.MCP_LIST, async (_, sid: string): Promise<McpServer[]> => {
    const meta = sessionStore.getSession(sid)
    // A missing session record is NOT "no servers configured". Throw so the
    // renderer surfaces the real cause instead of the misleading empty state.
    if (!meta) {
      throw new Error(`Session record not found (sid=${sid}), unable to start MCP backend`)
    }
    let bridge = bridges.get(sid)
    if (!bridge || !bridge.isRunning) {
      bridge = await ensureBridge(sid, meta.cwd, resolveModelId(meta.model_id))
    }
    // Distinguish "Python failed to start" from "no servers configured".
    // Returning [] here would render as the "no MCP servers configured" empty state,
    // hiding a real spawn failure behind a misleading message. Throw instead so
    // the renderer surfaces the error rather than a false "not configured".
    if (!bridge || !bridge.isRunning) {
      throw new Error('MCP backend process failed to start (Python agent not ready)')
    }
    return (await bridge.listMcp()) as McpServer[]
  })

  // Toggle `disabled` on a server in <cwd>/.cluxmate/mcp.json. Always writes
  // to the project file — the Python side deep-merges project over global, so
  // disabling a globally-configured server creates a project entry that
  // overrides. Server name is validated on the Python side at config load;
  // re-check here as defense in depth (renderer can pass arbitrary strings).
  // Does NOT hot-reload the running Python process — next `initialize` picks
  // up the change (next session or explicit reload).
  ipcMain.handle(
    IPC.MCP_SET_DISABLED,
    async (_, sid: string, name: string, disabled: boolean): Promise<void> => {
      if (!/^[A-Za-z0-9_-]+$/.test(name)) {
        throw new Error('Invalid MCP server name')
      }
      const meta = sessionStore.getSession(sid)
      if (!meta) throw new Error('Session not found')
      const cfgDir = path.join(meta.cwd, '.cluxmate')
      const cfgPath = path.join(cfgDir, 'mcp.json')
      fs.mkdirSync(cfgDir, { recursive: true })
      let config: any = {}
      try { config = JSON.parse(fs.readFileSync(cfgPath, 'utf-8')) } catch {}
      if (!config.mcpServers || typeof config.mcpServers !== 'object') {
        config.mcpServers = {}
      }
      if (!config.mcpServers[name] || typeof config.mcpServers[name] !== 'object') {
        config.mcpServers[name] = {}
      }
      config.mcpServers[name].disabled = disabled
      fs.writeFileSync(cfgPath, JSON.stringify(config, null, 2), 'utf-8')
    }
  )

  // Git branch display/switch for the working-dir bar. Runs git directly in the
  // main process (see git-service.ts), independent of the per-session Python
  // bridge, so it works even when that process is cold. cwd comes from the
  // renderer (store.workingDir), matching how SKILL_LIST takes cwd.
  ipcMain.handle(IPC.GIT_INFO, async (_, cwd: string) => gitService.gitInfo(cwd))

  ipcMain.handle(IPC.GIT_BRANCHES, async (_, cwd: string) => gitService.gitBranches(cwd))

  ipcMain.handle(IPC.GIT_CHECKOUT, async (_, cwd: string, branch: string, strategy: GitCheckoutStrategy) => {
    try {
      return await gitService.checkout(cwd, branch, strategy)
    } catch (e: any) {
      return { ok: false, message: e?.message || 'git checkout failed' }
    }
  })

  ipcMain.handle(IPC.APP_VERSION, () => appVersion)

  ipcMain.handle(IPC.GET_DEFAULT_CWD, () => path.resolve(__dirname, '../../..'))

  ipcMain.handle(IPC.GET_MODELS_CONFIG, () => readModelsConfig())

  ipcMain.handle(IPC.SAVE_MODELS_CONFIG, (_, cfg: { models: ModelEntry[]; activeId: string }) => {
    const models = cfg.models || []
    let activeId = cfg.activeId || ''
    if (!models.some((m) => m.id === activeId)) activeId = models[0]?.id || ''
    const out = { version: 2, models, active_model_id: activeId }
    fs.writeFileSync(configPath(), JSON.stringify(out, null, 2), 'utf-8')

    // Kill the active session's bridge so the next chat picks up the new config.
    // Otherwise the Python process would keep using the old model/api_key until
    // the user manually restarts the session or creates a new one.
    if (activeSessionId) {
      const b = bridges.get(activeSessionId)
      if (b) {
        bridges.delete(activeSessionId)
        b.kill().catch(() => {})
      }
    }
  })

  // Update just config.json's active_model_id (the Settings "default model") when
  // the user picks a model in the composer. Deliberately does NOT kill the
  // bridge — the per-message model_id override already applies the model; this
  // field only drives the default for NEW sessions.
  ipcMain.handle(IPC.SET_DEFAULT_MODEL, (_, modelId: string) => {
    const { models } = readModelsConfig()
    if (!models.some((m) => m.id === modelId)) throw new Error('Model not found')
    const out = { version: 2, models, active_model_id: modelId }
    fs.writeFileSync(configPath(), JSON.stringify(out, null, 2), 'utf-8')
  })

  ipcMain.handle(IPC.SELECT_DIRECTORY, async () => {
    const win = getMainWindow()
    const result = await dialog.showOpenDialog(win, {
      properties: ['openDirectory'],
      title: 'Select Working Directory',
    })
    if (result.canceled || result.filePaths.length === 0) return null
    return result.filePaths[0]
  })

  ipcMain.handle(IPC.OPEN_EXTERNAL, async (_, filePath: string) => {
    await shell.openPath(filePath)
  })
}

export function killAllBridges() {
  for (const b of bridges.values()) {
    try { b.kill() } catch { /* ignore during shutdown */ }
  }
  bridges.clear()
}

// ── idle reaper ──────────────────────────────────────────────────────────
// Pre-warming spawns a Python process on every session open, which leaks
// resident processes as the user browses. The reaper periodically kills
// bridges that have been idle too long — while exempting:
//   - the currently-active session (the user is looking at it)
//   - any bridge mid-turn (_busy — may be waiting on a tool-approval prompt)
// A killed bridge is transparently respawned by ensureBridge on the next
// chat/send (or on switch-back, via background pre-warm). Respawn is cheap now
// that MCP loads off the critical path — the initialize handshake resolves in
// ~1.7s — so reaping aggressively costs little.

const IDLE_REAP_MS = 2 * 60 * 1000  // 2 minutes idle → reclaim
// Check interval tracks the threshold (~1/3) so the actual reap fires close to
// the configured idle time without polling wastefully. Floored so a very short
// threshold can't spin the timer too hot.
const REAP_INTERVAL_MS = Math.max(15_000, Math.round(IDLE_REAP_MS / 3))

function reapIdleBridges() {
  const now = Date.now()
  const killed: string[] = []
  for (const [sid, b] of bridges) {
    if (sid === activeSessionId) continue
    if (!b.isRunning) continue
    if (b._busy) continue
    if (now - b._lastActivityAt < IDLE_REAP_MS) continue
    console.log(`[reaper] killing idle bridge for ${sid} (idle ${Math.round((now - b._lastActivityAt) / 1000)}s)`)
    bridges.delete(sid)
    killed.push(sid)
    b.kill().catch(() => {})
  }
  // Notify the renderer so the sidebar dots reflect the reclaimed process
  // (otherwise they'd stay green until the next user-triggered refresh).
  if (killed.length > 0) {
    for (const win of BrowserWindow.getAllWindows()) {
      win.webContents.send(IPC.BRIDGE_STATUS_CHANGED, { sessionIds: killed, running: false })
    }
  }
}

let reaperTimer: NodeJS.Timeout | null = null
export function startIdleReaper() {
  if (reaperTimer) return
  reaperTimer = setInterval(reapIdleBridges, REAP_INTERVAL_MS)
  reaperTimer.unref?.()  // don't keep the app alive just for the reaper
}

export function stopIdleReaper() {
  if (reaperTimer) { clearInterval(reaperTimer); reaperTimer = null }
}
