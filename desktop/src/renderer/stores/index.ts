import { create } from 'zustand'
import type {
  SessionMeta, GroupMeta, ChatMessage, SessionStreamEvent,
  PermissionRequest, ChatResult, MessageBlock, ToolCallEntry, AgentNode,
  Checkpoint, CheckpointFileDiff, SkillMeta, McpServer, BatchEditRequest,
  ModelEntry, PermissionMode, GitInfo, ReplaySubagent, TurnContext,
  PendingQuestion, QuestionAnswer, SessionSearchHit, HookEntry, HookRunEntry,
} from '../../shared/types'
import { deriveSessionTitle } from '../../shared/session-title'
import { defaultReasoningValue } from '../../shared/reasoning'
import { editsFromToolInput } from '../components/MultiEditDiff'
import { saveTheme, DEFAULT_THEME } from '../themes'
import { saveFontFamily, saveFontSize, loadFontFamily, loadFontSize } from '../fonts'
import { applyLang, loadLang, saveLang, tGlobal, type Lang } from '../i18n'

// Replacement agent reply shown when the turn ends in a non-recoverable error
// (process crash / external kill mid-stream). The partial stream is discarded
// wholesale — it was never a complete answer and must not be mistaken for one,
// nor should it be recorded as history. Translated via the active UI language.
const TURN_ERROR_TEXT = (): string => tGlobal('error.unexpected')

// Max render cadence for streamed text/thinking deltas. ~25fps: smooth to the
// eye while collapsing hundreds of per-token set() calls into a bounded rate so
// markdown re-parse of the growing block can't saturate the main thread. Only
// caps rendering — deltas are applied to state immediately, so no text is lost.
const RENDER_THROTTLE_MS = 40

// Trailing-edge cadence for persisting the display transcript during a live
// stream. The display is written as the turn progresses (not only at turn end)
// so a client kill mid-turn leaves the partial turn on disk — mirroring the
// Python JSONL's incremental persistence.
const DISPLAY_PERSIST_MS = 500

// ── session search debounce ──
// Keystrokes coalesce into one backend disk scan per SEARCH_DEBOUNCE_MS; the
// module-level `searchTimer` + monotonic `searchSeq` guard against out-of-order
// completions (a slow older scan cannot clobber a newer query's results).
const SEARCH_DEBOUNCE_MS = 200
let searchTimer: ReturnType<typeof setTimeout> | null = null
let searchSeq = 0

let _msgId = 0
function nextId(): string { return `msg-${++_msgId}` }

// Normalized working-directory equality for the "one empty session per project"
// rule: strips trailing separators and treats backslash/forward-slash as
// equivalent, so one directory under different spellings counts as one project.
function sameCwd(a: string | undefined, b: string): boolean {
  const norm = (p: string) => (p || '').replace(/[\\/]+$/, '').replace(/\\/g, '/')
  return norm(a || '') === norm(b)
}

// --- ordered-block helpers -------------------------------------------------
// An agent message holds an ordered list of text/tool blocks so text and tool
// cards render in the exact sequence the agent produced them.

function appendText(msg: ChatMessage, delta: string): ChatMessage {
  const blocks = [...(msg.blocks || [])]
  const last = blocks[blocks.length - 1]
  if (last && last.type === 'text') {
    blocks[blocks.length - 1] = { type: 'text', text: last.text + delta }
  } else {
    blocks.push({ type: 'text', text: delta })
  }
  return { ...msg, blocks }
}

function addToolBlock(msg: ChatMessage, tool: ToolCallEntry): ChatMessage {
  return { ...msg, blocks: [...(msg.blocks || []), { type: 'tool', tool }] }
}

function patchTool(msg: ChatMessage, callId: string, patch: Partial<ToolCallEntry>): ChatMessage {
  return {
    ...msg,
    blocks: (msg.blocks || []).map((b): MessageBlock =>
      b.type === 'tool' && b.tool.call_id === callId
        ? { type: 'tool', tool: { ...b.tool, ...patch } }
        : b
    ),
  }
}

function blocksToText(blocks: MessageBlock[] | undefined): string {
  return (blocks || []).filter((b): b is Extract<MessageBlock, { type: 'text' }> => b.type === 'text')
    .map((b) => b.text).join('')
}

// Drop trailing text blocks (the current, possibly-rejected answer) while keeping
// interleaved tool blocks. Used by text_restart so a re-generated reply doesn't
// append to the discarded attempt. There is at most one trailing text block, but
// trim in a loop to stay correct against any future block ordering.
function trimTrailingText(blocks: MessageBlock[]): MessageBlock[] {
  const out = [...blocks]
  while (out.length > 0 && out[out.length - 1].type === 'text') out.pop()
  return out
}

function mapAgentMsg(msgs: ChatMessage[], id: string, fn: (m: ChatMessage) => ChatMessage): ChatMessage[] {
  return msgs.map((m) => (m.id === id ? fn(m) : m))
}

// --- subagent node helpers -------------------------------------------------
// A subagent node holds its own ordered block list, mirroring the root message.
// These update one node inside a message's `subagents` map immutably.

function updateNode(
  msg: ChatMessage, agentId: string, fn: (n: AgentNode) => AgentNode
): ChatMessage {
  const subagents = { ...(msg.subagents || {}) }
  const node = subagents[agentId]
  if (!node) return msg
  subagents[agentId] = fn(node)
  return { ...msg, subagents }
}

function nodeAppendText(node: AgentNode, delta: string): AgentNode {
  const blocks = [...node.blocks]
  const last = blocks[blocks.length - 1]
  if (last && last.type === 'text') {
    blocks[blocks.length - 1] = { type: 'text', text: last.text + delta }
  } else {
    blocks.push({ type: 'text', text: delta })
  }
  return { ...node, blocks }
}

function nodeAddTool(node: AgentNode, tool: ToolCallEntry): AgentNode {
  return { ...node, blocks: [...node.blocks, { type: 'tool', tool }] }
}

function nodePatchTool(node: AgentNode, callId: string, patch: Partial<ToolCallEntry>): AgentNode {
  return {
    ...node,
    blocks: node.blocks.map((b): MessageBlock =>
      b.type === 'tool' && b.tool.call_id === callId
        ? { type: 'tool', tool: { ...b.tool, ...patch } }
        : b
    ),
  }
}

// True when any message already carries a reconstructed subagent tree.
function hasSubagents(msgs: ChatMessage[]): boolean {
  return msgs.some((m) => m.subagents && Object.keys(m.subagents).length > 0)
}

// Attach replay nodes (each carries `turn` = the parent-session turn number) to
// the matching agent message: the Nth agent message is turn N. Nodes are keyed
// by agent_id with `parent_id` links, mirroring the live-stream tree.
function attachReplaySubagents(msgs: ChatMessage[], nodes: ReplaySubagent[]): ChatMessage[] {
  if (nodes.length === 0) return msgs
  const byTurn = new Map<number, ReplaySubagent[]>()
  for (const n of nodes) {
    const list = byTurn.get(n.turn) || []
    list.push(n)
    byTurn.set(n.turn, list)
  }
  let agentOrdinal = 0
  return msgs.map((m) => {
    if (m.role !== 'agent') return m
    agentOrdinal += 1
    const list = byTurn.get(agentOrdinal)
    if (!list || list.length === 0) return m
    const subagents = { ...(m.subagents || {}) }
    for (const n of list) {
      const { turn: _turn, ...node } = n
      subagents[node.agent_id] = node
    }
    return { ...m, subagents }
  })
}

interface SessionState {
  messages: ChatMessage[]
  isStreaming: boolean
  streamingContent: string
  thinkingContent: string
  pendingPermission: PermissionRequest | null
  pendingBatchEdit: BatchEditRequest | null
  pendingQuestion: PendingQuestion | null
  draftText: string
  // The session's selected config model entry id + reasoning level. Both ride
  // every chat/send so the Python agent switches provider/effort per message.
  modelId: string
  reasoningEffort: string | null
  // True when a turn completed while this session was in the background and the
  // user hasn't switched back to view the result yet. Drives the sidebar's green
  // "unread" dot (distinct from the streaming "working" indicator).
  hasUnread: boolean
}

// Which subagent node the inspector panel is showing, if any.
export interface SelectedAgent {
  messageId: string
  agentId: string
}

interface AppState {
  sessions: SessionMeta[]
  groups: GroupMeta[]
  // ── session full-text search ──
  // `searchQuery` is the live input (not lowercased/debounced); `searchResults`
  // is null when not searching (render the normal grouped list) or the latest
  // hits once a debounced query has resolved. A stale-response guard in
  // setSearchQuery prevents an older in-flight result from clobbering a newer one.
  searchQuery: string
  searchResults: SessionSearchHit[] | null
  activeSessionId: string | null
  sessionStates: Map<string, SessionState>
  // derived
  messages: ChatMessage[]
  isStreaming: boolean
  streamingContent: string
  thinkingContent: string
  pendingPermission: PermissionRequest | null
  pendingBatchEdit: BatchEditRequest | null
  pendingQuestion: PendingQuestion | null
  // global
  workingDir: string
  // The cwd of the session the user most recently SENT a message in. New
  // sessions default to this (rather than `workingDir`, which tracks the
  // currently-viewed session — a session the user merely browsed without
  // sending anything should not hijack the default). Falls back to workingDir
  // (or the initial default cwd) until the first send.
  lastSentCwd: string | null
  // Git state for the current working directory (see refreshGitInfo).
  git: GitInfo | null
  // Track the current stream-unsubscribe function so switchSession (and any
  // other action that replaces the active session) can tear down the listener
  // BEFORE removing the session's messages. Otherwise an active stream keeps
  // firing events against a stale session state, causing jank/reconciliation
  // storms on every incoming chunk.
  _activeUnsub: (() => void) | null
  // Configurable model entries + the globally-active one. New sessions snapshot
  // the active entry; existing sessions keep the model they were created with.
  models: ModelEntry[]
  // The config.json active model — the default for NEW sessions and what the
  // Settings "active" radio edits. Distinct from `activeModelId`, which tracks
  // the active session's own composer selection.
  defaultModelId: string
  activeModelId: string
  // The active session's currently-selected reasoning level (mirrors its
  // SessionState.reasoningEffort so the composer + Settings read one source).
  activeReasoningEffort: string | null
  // Active UI theme id (see renderer/themes.ts). Persisted to localStorage.
  theme: string
  // Code font family id + global UI scale (see renderer/fonts.ts). Persisted.
  fontFamily: string
  fontSize: number
  // UI language (see renderer/i18n.ts): 'en' | 'zh'. Persisted to localStorage.
  lang: Lang
  // Development mode: plan (read-only) / default / acceptEdits (writes auto) /
  // yolo (everything auto, incl. dangerous). Per-session, not persisted.
  mode: PermissionMode
  error: string | null
  // Per-session Python process liveness, refreshed on key events.
  bridgeStatuses: Record<string, boolean>
  // Which subagent node the right-side inspector is showing (null = hidden).
  selectedAgent: SelectedAgent | null
  // Checkpoint timeline panel: open state + loaded list for the active session.
  checkpointsOpen: boolean
  checkpoints: Checkpoint[]
  // Context inspector panel: open state + reconstructed per-turn contexts.
  contextOpen: boolean
  turnContexts: TurnContext[]
  contextLoading: boolean
  // Whose context the panel is showing: null = the parent (main) session, or a
  // subagent node (sessionId = its agent_id, messageId = the parent message
  // whose tree owns it). Set when the panel is opened from the Agent Tree; the
  // header "Context History" button always resets to null. `messageId` lets
  // "back to Agent Tree" re-select the exact node it came from.
  contextTarget: { sessionId: string; label: string; messageId: string } | null
  // Read-only "focus" view: when set, the main conversation area shows this
  // subagent's conversation (with a lineage breadcrumb) instead of the parent
  // transcript. Shares the {messageId, agentId} shape with selectedAgent, but is
  // independent of the right-side dock. null = normal parent transcript.
  focusAgent: SelectedAgent | null
  // Diff preview shown in the right-side dock (shared with the agent tree /
  // checkpoint timeline). Opened from the inline changed-files card or the
  // timeline; null = not showing. checkpointId + activePath identify what's
  // shown so openDiff can toggle (click same file again → close).
  diffView: { checkpointId: string; label: string; files: CheckpointFileDiff[]; activePath?: string } | null
  // Right-click context menu over the chat area. x/y are viewport coords;
  // `selection` is the plain selected text, `markdown` the same range as its
  // markdown source (empty when nothing is selected). null = hidden.
  contextMenu: { x: number; y: number; selection: string; markdown: string } | null
  contextMenuTarget: { type: 'session' | 'group'; id: string; groupId?: string | null; provider?: string; model?: string; isPinned?: boolean } | null
  // Which sidebar item is showing an inline rename input. Lifted into the store
  // (rather than SessionList-local state) so the right-click context menu — a
  // sibling component — can start a rename inline for the target session/group.
  editingSessionId: string | null
  editingGroupId: string | null
  // One-shot draft to load into the input box (e.g. the undone message's text).
  // InputBox consumes it on change, then clears it via consumeInputDraft.
  inputDraft: string | null
  // Which view the main area shows: the chat, the skills browser, MCP, or Settings.
  mainView: 'chat' | 'skills' | 'mcp' | 'settings' | 'hooks'
  // Skills browser state: discovered skills, the selected one's path, and its
  // loaded SKILL.md content.
  skills: SkillMeta[]
  selectedSkillPath: string | null
  skillContent: string
  // MCP browser state: discovered servers (from per-session Python process)
  // and the selected one's name. Unlike skills, server list is per-session
  // (each session has its own Python process owning MCP subprocesses).
  mcpServers: McpServer[]
  selectedMcpServer: string | null
  // True while MCP_LIST is in-flight so the UI can show a loading state
  // during bridge warm-up (cold session just switched to).
  mcpLoading: boolean
  // Lifecycle hooks (settings.json) active in the current session's project,
  // normalized by the Python side (global + project merged).
  hooks: HookEntry[]
  hooksLoading: boolean

  // actions
  loadSessions: () => Promise<void>
  setSearchQuery: (q: string) => void
  clearSearch: () => void
  initConfig: () => Promise<void>
  setTheme: (id: string) => void
  setFontFamily: (id: string) => void
  setFontSize: (size: number) => void
  setLanguage: (lang: Lang) => void
  setWorkingDir: (dir: string) => void
  refreshGitInfo: () => Promise<void>
  createSession: (cwd?: string) => Promise<void>
  deleteSession: (id: string) => Promise<void>
  createGroup: (name: string) => Promise<void>
  renameGroup: (id: string, name: string) => Promise<void>
  deleteGroup: (id: string) => Promise<void>
  moveSession: (sessionId: string, groupId: string | null) => Promise<void>
  moveSessionToProject: (sessionId: string) => Promise<void>
  renameSession: (id: string, title: string) => Promise<void>
  pinSession: (id: string, pinned: boolean) => Promise<void>
  switchSession: (id: string) => Promise<void>
  sendMessage: (text: string) => Promise<void>
  cancelChat: () => Promise<void>
  approveTool: (callId: string, always?: boolean) => Promise<void>
  denyTool: (callId: string) => Promise<void>
  approveBatchEdit: (callId: string, always?: boolean) => Promise<void>
  answerQuestion: (callId: string, answers: QuestionAnswer[]) => Promise<void>
  setMode: (mode: PermissionMode) => Promise<void>
  // Select the active session's model + reasoning level (the composer seat).
  selectModel: (modelId: string, reasoningEffort: string | null) => Promise<void>
  refreshPermissions: () => Promise<void>
  refreshBridgeStatuses: () => Promise<void>
  selectAgent: (sel: SelectedAgent | null) => void
  toggleCheckpoints: (open?: boolean) => void
  loadCheckpoints: () => Promise<void>
  toggleContext: (open?: boolean) => void
  loadTurnContexts: (targetSessionId?: string | null) => Promise<void>
  // Open the context panel scoped to a subagent node (agent_id == its session
  // id / JSONL filename), replacing the agent-tree inspector in the right dock.
  viewSubagentContext: (messageId: string, agentId: string, label: string) => void
  // From a subagent's context panel, re-open the Agent Tree at the node it came
  // from (re-selects the same message + agent the user drilled into).
  backToAgentTree: () => void
  // Show a subagent's conversation in the MAIN area (read-only focus view),
  // replacing the parent transcript. Closes the right-side dock (the tree) so
  // the subagent isn't shown in both places at once.
  focusSubagent: (messageId: string, agentId: string) => void
  // Return from a subagent focus view to the parent session's transcript.
  clearFocus: () => void
  // From the focus view, return to the main transcript AND open the Agent Tree
  // (right dock) at the node the user was viewing.
  openAgentTreeFromFocus: () => void
  restoreCheckpoint: (checkpointId: string) => Promise<void>
  undoMessage: (messageId: string) => Promise<void>
retryMessage: (messageId: string) => Promise<void>
  consumeInputDraft: () => void
  setDraft: (sessionId: string, text: string) => void
  openDiff: (checkpointId: string, label: string, path?: string) => Promise<void>
  // Preview an edit tool's changes directly from disk (no checkpoint needed):
  // current file content is the "after", the "before" is reconstructed by
  // reversing the applied edits. Opens the right-dock diff at `activePath`.
  previewEditFiles: (key: string, edits: { path: string; old_string: string; new_string: string }[], activePath: string) => Promise<void>
  setDiffActive: (path: string) => void
  closeDiff: () => void
  openContextMenu: (menu: { x: number; y: number; selection: string; markdown: string; target?: { type: 'session' | 'group'; id: string; groupId?: string | null; provider?: string; model?: string; isPinned?: boolean } }) => void
  closeContextMenu: () => void
  startEditSession: (id: string) => void
  cancelEditSession: () => void
  startEditGroup: (id: string) => void
  cancelEditGroup: () => void
  showSkills: () => Promise<void>
  loadSkills: () => Promise<void>
  showChat: () => void
  showSettings: () => void
  selectSkill: (path: string) => Promise<void>
  setSkillDisabled: (slug: string, disabled: boolean) => Promise<void>
  showMcp: () => Promise<void>
  selectMcpServer: (name: string) => void
  setMcpDisabled: (name: string, disabled: boolean) => Promise<void>
  showHooks: () => Promise<void>
  reloadHooks: () => Promise<void>
  notifyHooks: (message: string) => Promise<void>
  clearError: () => void
  setError: (msg: string | null) => void
}

const NEW_SS: SessionState = {
  messages: [],
  isStreaming: false,
  streamingContent: '',
  thinkingContent: '',
  pendingPermission: null,
  pendingBatchEdit: null,
  pendingQuestion: null,
  draftText: '',
  modelId: '',
  reasoningEffort: null,
  hasUnread: false,
}

export const useStore = create<AppState>((set, get) => ({
  sessions: [],
  groups: [],
  searchQuery: '',
  searchResults: null,
  activeSessionId: null,
  sessionStates: new Map(),
  messages: [],
  isStreaming: false,
  streamingContent: '',
  thinkingContent: '',
  pendingPermission: null,
  pendingBatchEdit: null,
  pendingQuestion: null,
  workingDir: '',
  lastSentCwd: null,
  git: null,
  models: [],
  defaultModelId: '',
  activeModelId: '',
  activeReasoningEffort: null,
  theme: document.documentElement.getAttribute('data-theme') || DEFAULT_THEME,
  fontFamily: loadFontFamily(),
  fontSize: loadFontSize(),
  lang: applyLang(loadLang()),
  mode: 'default',
  error: null,
  bridgeStatuses: {},
  selectedAgent: null,
  checkpointsOpen: false,
  checkpoints: [],
  contextOpen: false,
  turnContexts: [],
  contextLoading: false,
  contextTarget: null,
  focusAgent: null,
  diffView: null,
  contextMenu: null,
  contextMenuTarget: null,
  editingSessionId: null,
  editingGroupId: null,
  inputDraft: null,
  mainView: 'chat',
  skills: [],
  selectedSkillPath: null,
  skillContent: '',
  mcpServers: [],
  selectedMcpServer: null,
  mcpLoading: false,
  hooks: [],
  hooksLoading: false,
  _activeUnsub: null,

  loadSessions: async () => {
    const [sessions, groups] = await Promise.all([
      window.electronAPI.listSessions(),
      window.electronAPI.listGroups(),
    ])
    set({ sessions, groups })
  },

  setSearchQuery: (q) => {
    const seq = ++searchSeq
    set({ searchQuery: q })
    const trimmed = q.trim()
    if (!trimmed) {
      set({ searchResults: null })
      return
    }
    if (searchTimer !== null) clearTimeout(searchTimer)
    searchTimer = setTimeout(async () => {
      try {
        const hits = await window.electronAPI.searchSessions(trimmed)
        if (seq === searchSeq) set({ searchResults: hits })
      } catch (e: any) {
        if (seq === searchSeq) set({ searchResults: [], error: tGlobal('error.searchFailed', { msg: e?.message }) })
      }
    }, SEARCH_DEBOUNCE_MS)
  },

  clearSearch: () => {
    searchSeq++
    if (searchTimer !== null) { clearTimeout(searchTimer); searchTimer = null }
    set({ searchQuery: '', searchResults: null })
  },

  createGroup: async (name) => {
    const group = await window.electronAPI.createGroup(name)
    set({ groups: [...get().groups, group] })
  },

  renameGroup: async (id, name) => {
    await window.electronAPI.renameGroup(id, name)
    set({
      groups: get().groups.map((p) =>
        p.id === id ? { ...p, name } : p
      ),
    })
  },

  deleteGroup: async (id) => {
    await window.electronAPI.deleteGroup(id)
    // The main process deleted every session inside the group along with it —
    // mirror that in local state (drop their session states, fix the active
    // pointer if it was one of them) and reload groups from the source of truth.
    const states = new Map(get().sessionStates)
    for (const s of get().sessions) {
      if (s.group_id === id) states.delete(s.id)
    }
    const remaining = get().sessions.filter((s) => s.group_id !== id)
    const activeGone = get().activeSessionId != null &&
      !remaining.some((s) => s.id === get().activeSessionId)
    const nextId = activeGone ? null : get().activeSessionId
    const ss = nextId ? states.get(nextId) : undefined
    const groups = await window.electronAPI.listGroups()
    set({
      groups,
      sessions: remaining,
      activeSessionId: nextId,
      sessionStates: states,
      messages: ss?.messages || [],
      isStreaming: ss?.isStreaming || false,
      streamingContent: ss?.streamingContent || '',
      thinkingContent: ss?.thinkingContent || '',
      pendingPermission: ss?.pendingPermission || null,
      pendingBatchEdit: ss?.pendingBatchEdit || null,
      pendingQuestion: ss?.pendingQuestion || null,
    })
    get().refreshBridgeStatuses()
  },

  moveSession: async (sessionId, groupId) => {
    await window.electronAPI.moveSession(sessionId, groupId)
    // Reload groups — an auto-group may have been cleaned up when emptied
    const groups = await window.electronAPI.listGroups()
    set({
      sessions: get().sessions.map((s) =>
        s.id === sessionId ? { ...s, group_id: groupId } : s
      ),
      groups,
    })
  },

  moveSessionToProject: async (sessionId) => {
    await window.electronAPI.moveSessionToProject(sessionId)
    // The target auto-group id is derived server-side from the session's cwd
    // (and may have been recreated), so reload both sessions and groups.
    const [groups, sessions] = await Promise.all([
      window.electronAPI.listGroups(),
      window.electronAPI.listSessions(),
    ])
    set({ groups, sessions })
  },

  renameSession: async (id, title) => {
    await window.electronAPI.renameSession(id, title)
    set({
      sessions: get().sessions.map((s) =>
        s.id === id ? { ...s, title } : s
      ),
    })
  },

  pinSession: async (id, pinned) => {
    // Optimistic local sort so the sidebar reorders instantly regardless of the
    // backend's eventual sort order (hot reload case; the backend already sorts
    // by is_pinned DESC). Fire-and-forget persist.
    window.electronAPI.pinSession(id, pinned).catch(() => {})
    set({
      sessions: get().sessions.map((s) =>
        s.id === id ? { ...s, is_pinned: pinned } : s
      ).sort((a, b) => {
        if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1
        return b.updated_at.localeCompare(a.updated_at)
      }),
    })
  },

  initConfig: async () => {
    const [cwd, cfg] = await Promise.all([
      window.electronAPI.getDefaultCwd(),
      window.electronAPI.getModelsConfig(),
    ])
    const activeModel = cfg.models.find((m) => m.id === cfg.activeId)
    set({
      workingDir: cwd,
      models: cfg.models,
      defaultModelId: cfg.activeId,
      activeModelId: cfg.activeId,
      activeReasoningEffort: defaultReasoningValue(activeModel),
    })
    // Permissions are per-project and loaded when a session becomes active
    // (switchSession / createSession), so nothing to fetch here.
    get().refreshGitInfo()
  },

  setTheme: (id) => {
    set({ theme: saveTheme(id) })
  },

  setFontFamily: (id) => {
    set({ fontFamily: saveFontFamily(id) })
  },

  setFontSize: (size) => {
    set({ fontSize: saveFontSize(size) })
  },

  setLanguage: (lang) => {
    set({ lang: saveLang(lang) })
  },

  setWorkingDir: async (dir: string) => {
    const sid = get().activeSessionId
    set({
      workingDir: dir,
      sessions: get().sessions.map((s) =>
        s.id === sid ? { ...s, cwd: dir } : s
      ),
    })
    // Update DB so the next CHAT_SEND picks up the new cwd, and the session
    // moves to the group matching the new cwd basename.
    if (sid) {
      try { await window.electronAPI.updateSessionCwd(sid, dir) } catch {}
      // Reload groups + sessions so the sidebar reflects the new group
      // assignment (updateSessionCwd now re-assigns group_id on the DB row).
      const [groups, sessions] = await Promise.all([
        window.electronAPI.listGroups(),
        window.electronAPI.listSessions(),
      ])
      if (groups) set({ groups })
      if (sessions) set({ sessions })
    }
    get().refreshGitInfo()
  },

  refreshGitInfo: async () => {
    const cwd = get().workingDir
    if (!cwd) { set({ git: null }); return }
    try {
      const git = await window.electronAPI.getGitInfo(cwd)
      // Guard against an out-of-order response: only apply if the working dir
      // hasn't changed since the request fired (mirrors selectSkill).
      if (get().workingDir === cwd) set({ git })
    } catch {
      // Leave stale git state rather than clobber with an error toast on a
      // benign background refresh (e.g. git missing on PATH).
      if (get().workingDir === cwd) set({ git: null })
    }
  },

  createSession: async (cwdOverride?: string) => {
    const { workingDir, lastSentCwd, models, defaultModelId, activeSessionId, sessions, sessionStates } = get()
    // An explicit override (the sidebar project "+") wins; otherwise new sessions
    // inherit the cwd of the session that last sent a message (falling back to
    // the currently-viewed session's dir, then the app default).
    const cwd = cwdOverride || lastSentCwd || workingDir
    // If an empty session already has an unsent draft in THIS project (same cwd),
    // jump back to it instead of creating another session (InputBox restores the
    // draft on switch). Each project keeps its own at most one empty session.
    for (const s of sessions) {
      if (!sameCwd(s.cwd, cwd)) continue
      const ss = sessionStates.get(s.id)
      if (ss && ss.draftText.trim() !== '' && ss.messages.length === 0) {
        if (s.id !== activeSessionId) await get().switchSession(s.id)
        return
      }
    }
    const entry = models.find((m) => m.id === defaultModelId) || models[0]
    if (!entry) {
      set({ error: tGlobal('error.noModel') })
      return
    }
    // New sessions preselect the model's reasoning default (preset or override).
    const entryEffort = defaultReasoningValue(entry)
    const [meta, groups] = await Promise.all([
      window.electronAPI.createSession({
        cwd,
        modelId: entry.id,
        apiType: entry.api_type,
        provider: entry.provider,
        model: entry.model_name,
      }),
      // Reload groups — a new auto-group may have been created for this cwd
      window.electronAPI.listGroups(),
    ])
    // The backend may have reused an existing empty session rather than creating
    // a new one — in that case just switch to it (it's already in `sessions`).
    if (sessions.some((s) => s.id === meta.id)) {
      if (meta.id !== activeSessionId) await get().switchSession(meta.id)
      return
    }
    const states = new Map(get().sessionStates)
    states.set(meta.id, { ...NEW_SS, messages: [], modelId: entry.id, reasoningEffort: entryEffort })

    // Insert after the last pinned session so the new session appears at the top
    // of unpinned sessions, not above pinned ones.
    const all = get().sessions
    let insertAt = 0
    for (let i = all.length - 1; i >= 0; i--) {
      if (all[i].is_pinned) { insertAt = i + 1; break }
    }
    const ordered = [...all.slice(0, insertAt), meta, ...all.slice(insertAt)]

    set({
      sessions: ordered,
      groups,
      activeSessionId: meta.id,
      activeModelId: entry.id,
      activeReasoningEffort: entryEffort,
      workingDir: meta.cwd,
      sessionStates: states,
      messages: [],
      isStreaming: false,
      streamingContent: '',
      thinkingContent: '',
      pendingPermission: null,
      pendingBatchEdit: null,
      pendingQuestion: null,
      selectedAgent: null,
      error: null,
    })
    // Load this project's saved policy (a prior session in the same cwd may
    // have enabled accept-edits).
    get().refreshPermissions()
    // New session pre-warms a bridge in background — reflect it when it's ready.
    get().refreshBridgeStatuses()
  },

  deleteSession: async (id) => {
    await window.electronAPI.deleteSession(id)
    const states = new Map(get().sessionStates)
    states.delete(id)
    const nextId = get().activeSessionId === id ? null : get().activeSessionId
    const ss = nextId ? states.get(nextId) : undefined
    // Reload groups — an auto-group may have been cleaned up
    const groups = await window.electronAPI.listGroups()
    set({
      sessions: get().sessions.filter((x) => x.id !== id),
      groups,
      activeSessionId: nextId,
      sessionStates: states,
      messages: ss?.messages || [],
      isStreaming: ss?.isStreaming || false,
      streamingContent: ss?.streamingContent || '',
      thinkingContent: ss?.thinkingContent || '',
      pendingPermission: ss?.pendingPermission || null,
      pendingBatchEdit: ss?.pendingBatchEdit || null,
      pendingQuestion: ss?.pendingQuestion || null,
    })
    // Bridge for the deleted session is now gone — refresh sidebar dots.
    get().refreshBridgeStatuses()
  },

  switchSession: async (id) => {
    // When switching away from a running session, DO NOT kill its stream
    // listener. The listener's `commit()` helper already guards:
    //   - events for a different sessionId are dropped early
    //   - messages/pendingPermission are only pushed to the UI when that
    //     session IS the active one
    //   - sessionStates is ALWAYS updated, so switching back shows the live
    //     progress accumulated while away
    // The listener self-clears in its `finally` block when the turn ends.

    let result
    try {
      result = await window.electronAPI.switchSession(id)
    } catch (e: any) {
      set({ error: tGlobal('error.openSessionFailed', { msg: e?.message }) })
      return
    }
    if (!result) return

    const states = new Map(get().sessionStates)
    let ss = states.get(id)
    if (!ss) { ss = { ...NEW_SS, messages: [] }; states.set(id, ss) }

    // Switching to a session is viewing it — clear any "unread completion" flag.
    ss.hasUnread = false

    // Only restore persisted messages if the in-memory session is empty.
    if (ss.messages.length === 0) {
      const display = result.display || []
      if (display.length > 0) {
        // Preferred path: the desktop-owned display transcript preserves text
        // + tool cards in order. Reassign fresh ids to avoid collisions.
        ss.messages = display.map((m) => ({ ...m, id: nextId() }))
      } else {
        // Fallback for sessions created before display transcripts existed:
        // reconstruct plain text from the provider-native history.
        const history = result.history || []
        for (const msg of history) {
          const role = msg.role as string
          if (role === 'user') {
            ss.messages.push({ id: nextId(), role: 'user', content: typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content), timestamp: Date.now() })
          } else if (role === 'assistant') {
            const content = typeof msg.content === 'string' ? msg.content : Array.isArray(msg.content) ? msg.content.filter((b: any) => b.type === 'text').map((b: any) => b.text).join('\n') : ''
            if (content) ss.messages.push({ id: nextId(), role: 'agent', content, timestamp: Date.now() })
          }
        }
      }
    }

    const meta = get().sessions.find((m) => m.id === id)
    // Populate a fresh session's model/effort selection from its persisted row
    // (model_id + reasoning_effort), falling back to the global active model and
    // that model's default effort for rows created before this schema.
    if (!ss.modelId) {
      const modelId = meta?.model_id || get().activeModelId
      const entry = get().models.find((m) => m.id === modelId) || get().models[0]
      ss.modelId = entry?.id || modelId
      ss.reasoningEffort = meta?.reasoning_effort != null ? meta.reasoning_effort : defaultReasoningValue(entry)
    }
    set({
      activeSessionId: id,
      activeModelId: ss.modelId,
      activeReasoningEffort: ss.reasoningEffort,
      workingDir: meta?.cwd || get().workingDir,
      sessionStates: states,
      messages: ss.messages,
      isStreaming: ss.isStreaming,
      streamingContent: ss.streamingContent,
      thinkingContent: ss.thinkingContent,
      pendingPermission: ss.pendingPermission,
      pendingBatchEdit: ss.pendingBatchEdit,
      pendingQuestion: ss.pendingQuestion,
      selectedAgent: null,
      contextOpen: false,
      turnContexts: [],
      contextTarget: null,
      focusAgent: null,
    })
    // Permissions are per-project — reload for the newly active session's cwd.
    get().refreshPermissions()
    // Refresh bridge status so the sidebar dots reflect the new active session.
    get().refreshBridgeStatuses()
    // Refresh git branch state for the newly active session's cwd.
    get().refreshGitInfo()
    // Fire-and-forget: if the restored transcript has no subagent tree, fetch
    // one from the authoritative Python JSONL. The bridge pre-warm is already
    // in flight; this resolves when it's ready and fills the tree in place.
    if (!hasSubagents(ss.messages)) {
      void (async () => {
        try {
          const { subagents } = await window.electronAPI.replaySession(id)
          if (!subagents || subagents.length === 0) return
          const cur = get()
          const states2 = new Map(cur.sessionStates)
          const s2 = states2.get(id)
          if (!s2) return
          s2.messages = attachReplaySubagents(s2.messages, subagents)
          set({
            sessionStates: states2,
            ...(cur.activeSessionId === id ? { messages: s2.messages } : {}),
          })
        } catch {
          // Bridge may be cold or the session has no subagent logs — the tree
          // is a nice-to-have; a missing one degrades to the existing view.
        }
      })()
    }
  },

  sendMessage: async (text) => {
    const sid = get().activeSessionId
    if (!sid) return

    // Record the sent-from cwd so a later "New Session" inherits the directory
    // of the session that last sent a message (not the currently-viewed one).
    const sentFrom = get().sessions.find((s) => s.id === sid)
    if (sentFrom?.cwd) set({ lastSentCwd: sentFrom.cwd })

    const userMsg: ChatMessage = { id: nextId(), role: 'user', content: text, timestamp: Date.now() }
    const agentMsgId = nextId()
    const agentMsg: ChatMessage = { id: agentMsgId, role: 'agent', content: '', blocks: [], timestamp: Date.now() }

    // Insert messages into session state
    const states = new Map(get().sessionStates)
    let ss = states.get(sid)
    if (!ss) { ss = { ...NEW_SS, messages: [] }; states.set(sid, ss) }
    ss.messages = [...ss.messages, userMsg, agentMsg]
    ss.isStreaming = true
    ss.streamingContent = ''
    ss.thinkingContent = ''
    ss.pendingPermission = null
    ss.pendingQuestion = null

    // Optimistically title the session after its first message. The backend
    // persists the same on CHAT_SEND; this just avoids waiting for a reload.
    const sessions = get().sessions.map((s) =>
      s.id === sid && (!s.title || s.title === 'New Session')
        ? { ...s, title: deriveSessionTitle(text) }
        : s
    )
    const bs0 = { ...get().bridgeStatuses, [sid]: true }
    set({ sessions, sessionStates: states, messages: ss.messages, isStreaming: true, streamingContent: '', thinkingContent: '', pendingPermission: null, pendingQuestion: null, error: null, bridgeStatuses: bs0 })

    // Pending throttled-render timer for this turn's text/thinking deltas (see
    // scheduleRender). Held across events so deltas coalesce into one render.
    let renderTimer: ReturnType<typeof setTimeout> | null = null

    const commit = (css: SessionState, s2: Map<string, SessionState>, extra?: Partial<AppState>) => {
      // Any explicit commit is a full flush — drop a pending throttled render so
      // it can't fire a redundant set() afterwards (this commit already carries
      // the latest css.messages, including any buffered text deltas).
      if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null }
      // Each commit carries the latest transcript — schedule a (throttled)
      // display persist so a mid-turn kill doesn't lose the turn.
      schedulePersist()
      const active = get().activeSessionId === sid
      const bridgeStatuses = { ...get().bridgeStatuses }
      bridgeStatuses[sid] = true
      set({
        sessionStates: s2,
        bridgeStatuses,
        ...(active ? { messages: css.messages, pendingPermission: css.pendingPermission, pendingBatchEdit: css.pendingBatchEdit, pendingQuestion: css.pendingQuestion } : {}),
        ...extra,
      })
    }

    // Throttle high-frequency text_delta/thinking renders. The delta is applied
    // to css.messages IMMEDIATELY (data never lags), but the React re-render is
    // coalesced to ~1 per RENDER_THROTTLE_MS. Without this, a fast stream fires
    // a set() per token — hundreds/sec — and even with memoized bubbles the one
    // growing block re-parses markdown each time, saturating the main thread and
    // freezing the UI. Trailing-edge: the first delta schedules a flush; deltas
    // arriving inside the window ride the already-scheduled render. A non-text
    // event (commit) or turn end flushes immediately, so ordering is preserved.
    const scheduleRender = () => {
      if (renderTimer !== null) return  // a flush is already pending; css is current
      renderTimer = setTimeout(() => {
        renderTimer = null
        const cur = get()
        const s3 = new Map(cur.sessionStates)
        const c3 = s3.get(sid)
        if (c3) commit(c3, s3)
      }, RENDER_THROTTLE_MS)
    }

    // Persist the display transcript on a trailing throttle while the turn
    // streams, so a client kill mid-turn still leaves the partial turn on disk.
    let persistTimer: ReturnType<typeof setTimeout> | null = null
    const schedulePersist = () => {
      if (persistTimer !== null) return
      persistTimer = setTimeout(() => {
        persistTimer = null
        const cur = get()
        const s3 = cur.sessionStates.get(sid)
        if (s3) window.electronAPI.saveDisplay(sid, s3.messages).catch(() => {})
      }, DISPLAY_PERSIST_MS)
    }

    const unsub = window.electronAPI.onStreamEvent((event) => {
      if (event.sessionId !== sid) return
      // Allow events from a background session to update sessionStates so that
      // switching back shows the live progress. The commit() helper below only
      // pushes messages/pendingPermission when this session IS the active one,
      // so the active session's UI is never polluted by background events.
      // The early bail on activeSessionId mismatch that was here before was
      // the cause of "switched back but the reply didn't load" — it silently
      // dropped every event for sessions running in the background.

      const current = get()
      const s2 = new Map(current.sessionStates)
      const css = s2.get(sid)
      if (!css) return

      // Events tagged with a non-root agent_id belong to a subagent node in
      // the tree, not the root message body.
      const aid = 'agent_id' in event ? (event as { agent_id?: string }).agent_id : undefined
      const isSub = !!aid && aid !== 'root'

      if (event.type === 'agent_start') {
        const node: AgentNode = {
          agent_id: event.agent_id, parent_id: event.parent_id,
          subagent_type: event.subagent_type, description: event.description,
          depth: event.depth, status: 'running', blocks: [],
          prompt: event.prompt,
        }
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) => ({
          ...m, subagents: { ...(m.subagents || {}), [event.agent_id]: node },
        }))
        // Subagent spawns only update the tree in place; they no longer
        // auto-open the inspector panel. The user opens it explicitly from the
        // reply when they want to inspect the tree.
        commit(css, s2)
      } else if (event.type === 'agent_end') {
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          updateNode(m, event.agent_id, (n) => ({
            ...n, status: event.status, result: event.result,
            input_tokens: event.input_tokens ?? n.input_tokens ?? 0,
            output_tokens: event.output_tokens ?? n.output_tokens ?? 0,
          }))
        )
        commit(css, s2)
      } else if (event.type === 'text_delta') {
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          isSub
            ? updateNode(m, aid!, (n) => nodeAppendText(n, event.content))
            : appendText(m, event.content)
        )
        // State is updated now; the render is throttled (high-frequency event).
        scheduleRender()
      } else if (event.type === 'thinking') {
        css.thinkingContent += event.content
        // Update the live message's thinking field in real-time so the
        // collapsible panel shows content as it streams — mirrors how
        // text_delta updates blocks on each chunk.  For subagents the
        // content goes into the node's thinking field; for the root it
        // goes directly on the message.
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          isSub
            ? updateNode(m, aid!, (n) => ({ ...n, thinking: (n.thinking || '') + event.content }))
            : { ...m, thinking: (m.thinking || '') + event.content }
        )
        // thinkingContent for the active session is picked up by the throttled
        // flush via the live sessionState; mirror it onto the top-level slice
        // there. Here we only need to schedule the (throttled) render.
        scheduleRender()
      } else if (event.type === 'tool_start') {
        // Subagents auto-approve, so their calls always start "running" and
        // never raise a permission prompt. Root keeps the approve/deny flow.
        const autoApproved = isSub || event.auto_approved === true || event.risk_level === 'safe'
        // For subagents, show every tool call in the node (so the tree/inspector
        // is complete). For root, hide safe read-only tools EXCEPT ones the
        // server flags visible (web_search/web_fetch) so a long network call
        // renders a running card instead of an invisible spinner.
        if (isSub || event.risk_level !== 'safe' || event.visible === true) {
          const tool: ToolCallEntry = {
            call_id: event.call_id, name: event.name,
            input: event.input as Record<string, unknown>,
            risk_level: event.risk_level,
            status: autoApproved ? 'running' : 'pending',
          }
          css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
            isSub
              ? updateNode(m, aid!, (n) => nodeAddTool(n, tool))
              : addToolBlock(m, tool)
          )
        }
        if (!isSub) {
          // Both multi_edit and single-file search_replace get the diff-preview
          // approval card. search_replace is normalized into a one-edit batch so
          // the two share the same review + result rendering.
          const edits = editsFromToolInput(event.name, event.input)
          if (edits && !autoApproved) {
            css.pendingBatchEdit = {
              call_id: event.call_id,
              tool_name: event.name,
              edits,
              risk_level: event.risk_level,
            }
          } else {
            css.pendingPermission = !autoApproved
              ? { call_id: event.call_id, tool_name: event.name, params: event.input as Record<string, unknown>, risk_level: event.risk_level, always_allowable: event.always_allowable, categories: event.categories }
              : css.pendingPermission
          }
        }
        commit(css, s2)
      } else if (event.type === 'question') {
        // ask_user_question paused for a human answer — show the inline
        // QuestionCard. The tool block (tool_start) already renders; the answer
        // flows back via answerQuestion and the tool_result resolves both.
        css.pendingQuestion = { call_id: event.call_id, questions: event.questions }
        commit(css, s2)
      } else if (event.type === 'tool_result') {
        const patch = { status: event.is_error ? ('error' as const) : ('done' as const), result: event.output }
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          isSub
            ? updateNode(m, aid!, (n) => nodePatchTool(n, event.call_id, patch))
            : patchTool(m, event.call_id, patch)
        )
        if (!isSub) { css.pendingPermission = null; css.pendingBatchEdit = null; css.pendingQuestion = null }
        commit(css, s2)
      } else if (event.type === 'turn_diff') {
        // Files this turn changed — attach to the root agent message so the
        // inline "changed files" card renders under the reply.
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) => ({
          ...m, turnDiff: { checkpoint_id: event.checkpoint_id, files: event.files },
        }))
        commit(css, s2)
      } else if (event.type === 'turn_start') {
        // Undo anchor for THIS turn — attach to the user message (not the agent
        // reply) so its per-message undo button can rewind files + history.
        css.messages = mapAgentMsg(css.messages, userMsg.id, (m) => ({
          ...m, undo: { checkpoint_id: event.checkpoint_id, log_seq: event.log_seq },
        }))
        commit(css, s2)
      } else if (event.type === 'title_suggested') {
        // Live-swap the sidebar title (main process already persisted it, with
        // the same don't-clobber-a-user-rename guard applied there).
        commit(css, s2, {
          sessions: get().sessions.map((s) =>
            s.id === sid ? { ...s, title: event.title } : s
          ),
        })
      } else if (event.type === 'skill_used') {
        // Annotate the turn with the skill used (slug-deduped so /slug + a
        // redundant use_skill call don't double up).
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) => {
          const existing = m.skillsUsed || []
          if (existing.some((s) => s.slug === event.slug)) return m
          return {
            ...m,
            skillsUsed: [...existing, {
              name: event.name, slug: event.slug,
              source: event.source, trigger: event.trigger,
            }],
          }
        })
        commit(css, s2)
      } else if (event.type === 'hook_result') {
        // Annotate the turn with each completed hook run (faint, like skillsUsed).
        const run: HookRunEntry = {
          event: event.event,
          tool_name: event.tool_name,
          command: event.command,
          blocked: event.blocked,
          reason: event.reason,
          feedback_count: event.feedback.length,
          error: event.error,
          duration_ms: event.duration_ms,
        }
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) => ({
          ...m, hooksUsed: [...(m.hooksUsed || []), run],
        }))
        commit(css, s2)
      } else if (event.type === 'text_restart') {
        // A Stop hook blocked the reply and the model is regenerating: clear the
        // rejected text/thinking so the new stream starts fresh.
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          isSub
            ? updateNode(m, aid!, (n) => ({ ...n, blocks: trimTrailingText(n.blocks), thinking: '' }))
            : { ...m, blocks: trimTrailingText(m.blocks || []), thinking: undefined }
        )
        css.streamingContent = ''
        css.thinkingContent = ''
        commit(css, s2)
      }
    })

    set({ _activeUnsub: unsub })

    const finalize = (mutate: (m: ChatMessage) => ChatMessage) => {
      const s3 = new Map(get().sessionStates)
      const sss = s3.get(sid)
      if (sss) {
        sss.messages = mapAgentMsg(sss.messages, agentMsgId, (m) => ({
          ...mutate(m),
          thinking: sss.thinkingContent || m.thinking,
        }))
        sss.isStreaming = false
        sss.streamingContent = ''
        sss.thinkingContent = ''
        // If the turn finished while this session was in the background, flag it
        // "unread" so the sidebar shows the green dot until the user views it.
        sss.hasUnread = get().activeSessionId !== sid
        // Persist the display transcript so reopening this session shows the
        // same text + tool cards in order — the provider-native history file
        // alone can't reconstruct the tool cards.
        window.electronAPI.saveDisplay(sid, sss.messages).catch(() => {})
      }
      const isActive = get().activeSessionId === sid
      // Update updated_at and re-sort sessions so the sidebar reflects the
      // latest activity timestamp and the active session moves to the top.
      // Also stamp the session with the model it actually sent (the composer's
      // per-session selection), so the sidebar label changes immediately.
      const entry = get().models.find((m) => m.id === ss.modelId)
      const now = new Date().toISOString()
      const sortedSessions = get().sessions.map((s) =>
        s.id === sid ? {
          ...s,
          updated_at: now,
          ...(entry ? {
            model_id: entry.id,
            model: entry.model_name || '',
            provider: entry.provider || '',
          } : {}),
        } : s
      ).sort((a, b) => {
        // Pinned first, then by updated_at desc
        if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1
        return b.updated_at.localeCompare(a.updated_at)
      })
      set({
        sessionStates: s3,
        messages: isActive && sss ? sss.messages : get().messages,
        isStreaming: isActive ? false : get().isStreaming,
        streamingContent: isActive ? '' : get().streamingContent,
        thinkingContent: isActive ? '' : get().thinkingContent,
        sessions: sortedSessions,
      })
    }

    try {
      const result: ChatResult = await window.electronAPI.sendMessage(sid, text, {
        modelId: ss.modelId,
        reasoningEffort: ss.reasoningEffort,
      })
      finalize((m) => {
        const txt = blocksToText(m.blocks) || result.text || ''
        const hasTools = (m.blocks || []).some((b) => b.type === 'tool')
        const blocks = (m.blocks && m.blocks.length > 0)
          ? m.blocks
          : txt ? [{ type: 'text' as const, text: txt }] : m.blocks
        const cacheUsage = result.usage?.input_tokens
          ? { input_tokens: result.usage.input_tokens, cache_read: result.usage.cache_read || 0, cache_write: result.usage.cache_write || 0 }
          : undefined
        return { ...m, blocks, content: txt || (hasTools ? '' : '(no output)'), cacheUsage, timing: result.timing }
      })
    } catch (e: any) {
      // A non-recoverable turn error (process crash / external kill) leaves a
      // partial, incomplete stream. Discard it wholesale — the half-written
      // answer and its thinking must not be mistaken for a real reply or leak
      // into history. Replace the whole agent message with a short error text.
      const s3 = new Map(get().sessionStates)
      const sss = s3.get(sid)
      if (sss) {
        sss.thinkingContent = ''
        sss.streamingContent = ''
      }
      set({ sessionStates: s3 })
      finalize((m) => ({
        ...m,
        blocks: [{ type: 'text', text: TURN_ERROR_TEXT() }],
        content: TURN_ERROR_TEXT(),
        thinking: undefined,
        subagents: undefined,
        turnDiff: undefined,
        skillsUsed: undefined,
        hooksUsed: undefined,
        cacheUsage: undefined,
        timing: undefined,
      }))
      set({ error: tGlobal('error.unexpected') })
    } finally {
      // Kill any pending throttled render — the turn is over (finalize already
      // pushed the final state via its own set()); a late timer would just fire
      // a redundant commit(). Harmless (it re-reads live state) but wasteful.
      if (renderTimer !== null) { clearTimeout(renderTimer); renderTimer = null }
      unsub()
      set({ _activeUnsub: null })
      // Bridge may have exited (crashed / finished) — refresh sidebar dots.
      get().refreshBridgeStatuses()
    }
  },

  cancelChat: async () => {
    const sid = get().activeSessionId
    if (!sid) return
    await window.electronAPI.cancelChat(sid)
    const states = new Map(get().sessionStates)
    const ss = states.get(sid)
    if (ss) { ss.isStreaming = false; ss.pendingPermission = null; ss.pendingBatchEdit = null; ss.pendingQuestion = null }
    set({ sessionStates: states, isStreaming: false, pendingPermission: null, pendingBatchEdit: null, pendingQuestion: null })
  },

  approveTool: async (callId, always = false) => {
    const sid = get().activeSessionId
    if (!sid) return
    const states = new Map(get().sessionStates)
    const ss = states.get(sid)
    if (ss) {
      ss.pendingPermission = null
      ss.messages = ss.messages.map((m) =>
        m.role === 'agent' ? patchTool(m, callId, { status: 'running' }) : m
      )
    }
    set({ sessionStates: states, pendingPermission: null, messages: ss ? ss.messages : get().messages })
    await window.electronAPI.approveTool(sid, callId, always)
  },

  denyTool: async (callId) => {
    const sid = get().activeSessionId
    if (!sid) return
    const states = new Map(get().sessionStates)
    const ss = states.get(sid)
    if (ss) {
      ss.pendingPermission = null
      ss.pendingBatchEdit = null
      ss.messages = ss.messages.map((m) =>
        m.role === 'agent' ? patchTool(m, callId, { status: 'denied' }) : m
      )
    }
    set({ sessionStates: states, pendingPermission: null, pendingBatchEdit: null, messages: ss ? ss.messages : get().messages })
    await window.electronAPI.denyTool(sid, callId)
  },

  approveBatchEdit: async (callId, always = false) => {
    const sid = get().activeSessionId
    if (!sid) return
    const states = new Map(get().sessionStates)
    const ss = states.get(sid)
    if (ss) {
      ss.pendingBatchEdit = null
      ss.messages = ss.messages.map((m) =>
        m.role === 'agent' ? patchTool(m, callId, { status: 'running' }) : m
      )
    }
    set({ sessionStates: states, pendingBatchEdit: null, messages: ss ? ss.messages : get().messages })
    // Whole-turn approval: no `selected` filter — MultiEditTool applies all edits.
    await window.electronAPI.approveTool(sid, callId, always)
  },

  answerQuestion: async (callId, answers) => {
    const sid = get().activeSessionId
    if (!sid) return
    const states = new Map(get().sessionStates)
    const ss = states.get(sid)
    if (ss) {
      ss.pendingQuestion = null
      // The tool block (from tool_start) is still "running" — keep it so the
      // subsequent tool_result flips it to "done"; we only dismiss the card.
      ss.messages = ss.messages.map((m) =>
        m.role === 'agent' ? patchTool(m, callId, { status: 'running' }) : m
      )
    }
    set({ sessionStates: states, pendingQuestion: null, messages: ss ? ss.messages : get().messages })
    await window.electronAPI.answerQuestion(sid, callId, answers)
  },

  setMode: async (mode) => {
    // Optimistic switch so the button feels instant. On failure, revert. The
    // Python side may rebuild the agent (plan changes the toolset), so this can
    // take a beat — but it's not persisted, so nothing to roll back on disk.
    const prev = get().mode
    set({ mode })
    const sid = get().activeSessionId
    if (!sid) return
    try {
      const p = await window.electronAPI.setMode(sid, mode)
      set({ mode: p.mode as PermissionMode })
    } catch {
      set({ mode: prev })
    }
  },

  selectModel: async (modelId, reasoningEffort) => {
    // Optimistic: update the active session's selection + the derived top-level
    // mirrors immediately, then persist to the session row (fire-and-forget-ish;
    // a failure just means the selection won't survive a restart).
    const sid = get().activeSessionId
    // A model change (not an effort-only change) also becomes the global default
    // shown in Settings, so new sessions start from the last-picked model.
    const modelChanged = modelId !== get().activeModelId
    const states = new Map(get().sessionStates)
    const ss = sid ? states.get(sid) : undefined
    if (ss) {
      ss.modelId = modelId
      ss.reasoningEffort = reasoningEffort
    }
    const entry = get().models.find((m) => m.id === modelId)
    set({
      activeModelId: modelId,
      activeReasoningEffort: reasoningEffort,
      ...(modelChanged ? { defaultModelId: modelId } : {}),
      ...(sid && ss ? { sessionStates: states } : {}),
      ...(sid && entry ? {
        sessions: get().sessions.map((s) =>
          s.id === sid
            ? { ...s, model_id: entry.id, model: entry.model_name || '', provider: entry.provider || '' }
            : s
        ),
      } : {}),
    })
    if (sid) {
      try {
        await window.electronAPI.setSessionModel(sid, modelId, reasoningEffort)
      } catch { /* keep the optimistic selection */ }
    }
    if (modelChanged) {
      try {
        await window.electronAPI.setDefaultModel(modelId)
      } catch { /* keep the optimistic default */ }
    }
  },

  refreshPermissions: async () => {
    const sid = get().activeSessionId
    if (!sid) { set({ mode: 'default' }); return }
    try {
      const p = await window.electronAPI.getPermissions(sid)
      set({ mode: p.mode as PermissionMode })
    } catch { /* leave current state */ }
  },

  refreshBridgeStatuses: async () => {
    const ids = get().sessions.map((s) => s.id)
    if (ids.length === 0) return
    try {
      const statuses = await window.electronAPI.getBridgeStatuses(ids)
      const current = get().bridgeStatuses
      const map: Record<string, boolean> = { ...current }
      for (const s of statuses) map[s.sessionId] = s.running || (current[s.sessionId] ?? false)
      set({ bridgeStatuses: map })
    } catch { /* best-effort — leave stale state */ }
  },

  // The agent inspector, checkpoint timeline, context inspector, and diff preview
  // all share the one right-dock slot — opening any one closes the others.
  selectAgent: (sel) => set({
    selectedAgent: sel,
    checkpointsOpen: sel ? false : get().checkpointsOpen,
    contextOpen: sel ? false : get().contextOpen,
    diffView: sel ? null : get().diffView,
  }),

  toggleCheckpoints: (open) => {
    const next = open ?? !get().checkpointsOpen
    set({
      checkpointsOpen: next,
      selectedAgent: next ? null : get().selectedAgent,
      contextOpen: next ? false : get().contextOpen,
      diffView: next ? null : get().diffView,
    })
    if (next) get().loadCheckpoints()
  },

  loadCheckpoints: async () => {
    const sid = get().activeSessionId
    if (!sid) { set({ checkpoints: [] }); return }
    try {
      const cps = await window.electronAPI.listCheckpoints(sid)
      set({ checkpoints: cps })
    } catch (e: any) {
      set({ error: tGlobal('error.loadCheckpoints', { msg: e?.message }) })
    }
  },

  // The header "Context History" button always inspects the MAIN agent, so
  // opening it here resets any subagent target. Closing keeps the target so a
  // reopen from the tree (viewSubagentContext) sets it explicitly.
  toggleContext: (open) => {
    const next = open ?? !get().contextOpen
    set({
      contextOpen: next,
      contextTarget: next ? null : get().contextTarget,
      selectedAgent: next ? null : get().selectedAgent,
      checkpointsOpen: next ? false : get().checkpointsOpen,
      diffView: next ? null : get().diffView,
      turnContexts: next ? [] : get().turnContexts,
    })
    if (next) get().loadTurnContexts(null)
  },

  // Open the context panel for a subagent node. Shares the right dock with the
  // agent-tree inspector, so selecting an agent closes it (and vice-versa).
  viewSubagentContext: (messageId, agentId, label) => {
    set({
      contextOpen: true,
      contextTarget: { sessionId: agentId, label, messageId },
      selectedAgent: null,
      checkpointsOpen: false,
      diffView: null,
      turnContexts: [],
    })
    get().loadTurnContexts(agentId)
  },

  // Back out of a subagent's context panel into the Agent Tree, re-selecting the
  // exact node the user drilled into. No-op without a subagent target (the main
  // agent's panel has no tree to return to).
  backToAgentTree: () => {
    const target = get().contextTarget
    if (!target) {
      get().toggleContext(false)
      return
    }
    set({
      selectedAgent: { messageId: target.messageId, agentId: target.sessionId },
      contextOpen: false,
      contextTarget: null,
      checkpointsOpen: false,
      diffView: null,
    })
  },

  // Show a subagent's conversation in the MAIN area (read-only focus view). The
  // subagent is still a one-shot delegate — this is navigation/reading, not a
  // resumable session — so it closes the right dock (tree) rather than leaving
  // the same node rendered in two places.
  focusSubagent: (messageId, agentId) => {
    set({
      focusAgent: { messageId, agentId },
      selectedAgent: null,
      contextOpen: false,
      checkpointsOpen: false,
      diffView: null,
    })
  },

  // Return to the parent session's transcript (focus view → main view).
  clearFocus: () => set({ focusAgent: null }),

  // From the focus view, return to the main transcript AND open the Agent Tree
  // at the node the user was viewing (so they land back where they drilled in).
  openAgentTreeFromFocus: () => {
    const focus = get().focusAgent
    if (!focus) { set({ focusAgent: null }); return }
    set({
      focusAgent: null,
      selectedAgent: { messageId: focus.messageId, agentId: focus.agentId },
      contextOpen: false,
      checkpointsOpen: false,
      diffView: null,
    })
  },

  // `targetSessionId` is whose log to reconstruct (a subagent id, or null/absent
  // for the active parent session). The IPC call always passes the parent
  // session id first so the main process resolves the right bridge process.
  loadTurnContexts: async (targetSessionId) => {
    const sid = get().activeSessionId
    if (!sid) { set({ turnContexts: [] }); return }
    set({ contextLoading: true })
    try {
      const res = await window.electronAPI.getTurnContexts(sid, targetSessionId ?? undefined)
      set({ turnContexts: res?.turns ?? [] })
    } catch (e: any) {
      set({ error: tGlobal('error.loadContext', { msg: e?.message }) })
    } finally {
      set({ contextLoading: false })
    }
  },

  restoreCheckpoint: async (checkpointId) => {
    const sid = get().activeSessionId
    if (!sid) return
    try {
      const res = await window.electronAPI.restoreCheckpoint(sid, checkpointId)
      // Workspace files changed on disk; refresh the timeline (restore adds a
      // new "before/after restore" checkpoint) so the list reflects it.
      await get().loadCheckpoints()
      if (res?.conflicts?.length) {
        set({ error: tGlobal('error.restoreConflicts', { files: res.conflicts.join(', ') }) })
      }
    } catch (e: any) {
      set({ error: tGlobal('error.restoreFailed', { msg: e?.message }) })
    }
  },

  // Undo a user message: rewind the workspace to the checkpoint taken before it
  // was sent, drop that message + everything after it (both provider history and
  // the display transcript), and refill the input box with the message's text so
  // the user can edit and resend. No-op if the message has no undo anchor (older
  // sessions, or checkpoints unavailable when it was sent).
  undoMessage: async (messageId) => {
    const sid = get().activeSessionId
    if (!sid) return
    const states = new Map(get().sessionStates)
    const ss = states.get(sid)
    if (!ss) return
    const idx = ss.messages.findIndex((m) => m.id === messageId)
    if (idx === -1) return
    const target = ss.messages[idx]
    if (!target.undo) return
    const draft = target.content

    let restoreRes
    try {
      // 1. Rewind agent-touched files to the pre-turn snapshot.
      restoreRes = await window.electronAPI.restoreCheckpoint(sid, target.undo.checkpoint_id)
      // 2. Truncate the provider-native history to the pre-turn length so the
      //    next send doesn't carry the undone turns as ghost context.
      await window.electronAPI.truncateHistory(sid, target.undo.log_seq)
    } catch (e: any) {
      set({ error: tGlobal('error.undoFailed', { msg: e?.message }) })
      return
    }
    if (restoreRes?.conflicts?.length) {
      set({ error: tGlobal('error.undoConflicts', { files: restoreRes.conflicts.join(', ') }) })
    }

    // 3. Drop this message + everything after it from the display transcript.
    const kept = ss.messages.slice(0, idx)
    ss.messages = kept
    ss.isStreaming = false
    ss.pendingPermission = null
    ss.pendingBatchEdit = null
    ss.pendingQuestion = null
    window.electronAPI.saveDisplay(sid, kept).catch(() => {})

    const isActive = get().activeSessionId === sid
    // 4. Refill the input box with the undone text.
    set({
      sessionStates: states,
      inputDraft: draft,
      ...(isActive ? { messages: kept, isStreaming: false, pendingPermission: null, pendingBatchEdit: null, pendingQuestion: null } : {}),
    })
    // 5. Refresh the checkpoint timeline if it's open (restore adds nodes).
    if (get().checkpointsOpen) get().loadCheckpoints()
  },

  // Retry the last agent reply: remove only the agent bubble (keep the user
  // message so nothing flashes), rewind files + history, then send the same
  // user text via the IPC and stream the new reply into a fresh agent
  // placeholder.  Does NOT call sendMessage (which would create a duplicate
  // user message).
  retryMessage: async (messageId) => {
    const sid = get().activeSessionId
    if (!sid || get().isStreaming) return

    // Retry is also a send — record the cwd so "New Session" inherits it.
    const sentFrom = get().sessions.find((s) => s.id === sid)
    if (sentFrom?.cwd) set({ lastSentCwd: sentFrom.cwd })
    const states = new Map(get().sessionStates)
    const ss = states.get(sid)
    if (!ss) return

    const idx = ss.messages.findIndex((m) => m.id === messageId)
    if (idx === -1) return
    const agentMsg = ss.messages[idx]
    if (agentMsg.role !== 'agent') return

    const userMsg = ss.messages[idx - 1]
    if (!userMsg || userMsg.role !== 'user') return
    const userText = userMsg.content

    // 1. Drop only the old agent reply — the user bubble stays in place.
    const kept = ss.messages.slice(0, idx)

    // 2. Append a fresh agent placeholder that will receive the new stream.
    const agentMsgId = nextId()
    const agentPlaceholder: ChatMessage = {
      id: agentMsgId, role: 'agent', content: '', blocks: [], timestamp: Date.now(),
    }
    ss.messages = [...kept, agentPlaceholder]
    ss.isStreaming = true
    ss.streamingContent = ''
    ss.thinkingContent = ''
    ss.pendingPermission = null
    ss.pendingBatchEdit = null
    ss.pendingQuestion = null
    window.electronAPI.saveDisplay(sid, ss.messages).catch(() => {})

    set({
      sessionStates: states,
      messages: ss.messages,
      isStreaming: true,
      streamingContent: '',
      thinkingContent: '',
      pendingPermission: null,
      pendingBatchEdit: null,
      pendingQuestion: null,
      bridgeStatuses: { ...get().bridgeStatuses, [sid]: true },
    })

    // 3. Rewind files + provider history (same undo anchors).
    if (userMsg.undo) {
      try {
        await window.electronAPI.restoreCheckpoint(sid, userMsg.undo.checkpoint_id)
        await window.electronAPI.truncateHistory(sid, userMsg.undo.log_seq)
      } catch (e: any) {
        set({ error: tGlobal('error.retryRewindFailed', { msg: e?.message }) })
        return
      }
    }
    if (get().checkpointsOpen) get().loadCheckpoints()

    // 4. Wire up the stream listener — same shape as sendMessage, but
    //    referencing the OLD user message for turn_start anchors and the
    //    NEW agent placeholder for everything else.
    // Persist the display transcript on a trailing throttle while the turn
    // streams (same crash-safety as sendMessage's schedulePersist).
    let persistTimer: ReturnType<typeof setTimeout> | null = null
    const schedulePersist = () => {
      if (persistTimer !== null) return
      persistTimer = setTimeout(() => {
        persistTimer = null
        const cur = get()
        const s3 = cur.sessionStates.get(sid)
        if (s3) window.electronAPI.saveDisplay(sid, s3.messages).catch(() => {})
      }, DISPLAY_PERSIST_MS)
    }
    const commit = (css: SessionState, s2: Map<string, SessionState>, extra?: Partial<AppState>) => {
      const active = get().activeSessionId === sid
      // Streaming events prove the bridge is alive — keep the sidebar dot green
      // without an extra IPC round-trip.
      schedulePersist()
      const bridgeStatuses = { ...get().bridgeStatuses }
      bridgeStatuses[sid] = true
      set({
        sessionStates: s2,
        bridgeStatuses,
        ...(active ? { messages: css.messages, pendingPermission: css.pendingPermission, pendingBatchEdit: css.pendingBatchEdit, pendingQuestion: css.pendingQuestion } : {}),
        ...extra,
      })
    }

    const unsub = window.electronAPI.onStreamEvent((event) => {
      if (event.sessionId !== sid) return
      // Same as sendMessage: allow background sessions to update sessionStates
      // so switching back during a retry shows the live progress.

      const current = get()
      const s2 = new Map(current.sessionStates)
      const css = s2.get(sid)
      if (!css) return

      const aid = 'agent_id' in event ? (event as { agent_id?: string }).agent_id : undefined
      const isSub = !!aid && aid !== 'root'

      if (event.type === 'agent_start') {
        const node: AgentNode = {
          agent_id: event.agent_id, parent_id: event.parent_id,
          subagent_type: event.subagent_type, description: event.description,
          depth: event.depth, status: 'running', blocks: [],
          prompt: event.prompt,
        }
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) => ({
          ...m, subagents: { ...(m.subagents || {}), [event.agent_id]: node },
        }))
        // Subagent spawns update the tree in place only — no auto-open of the
        // inspector panel (mirrors sendMessage).
        commit(css, s2)
      } else if (event.type === 'agent_end') {
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          updateNode(m, event.agent_id, (n) => ({
            ...n, status: event.status, result: event.result,
            input_tokens: event.input_tokens ?? n.input_tokens ?? 0,
            output_tokens: event.output_tokens ?? n.output_tokens ?? 0,
          }))
        )
        commit(css, s2)
      } else if (event.type === 'text_delta') {
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          isSub
            ? updateNode(m, aid!, (n) => nodeAppendText(n, event.content))
            : appendText(m, event.content)
        )
        commit(css, s2)
      } else if (event.type === 'thinking') {
        css.thinkingContent += event.content
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          isSub
            ? updateNode(m, aid!, (n) => ({ ...n, thinking: (n.thinking || '') + event.content }))
            : { ...m, thinking: (m.thinking || '') + event.content }
        )
        commit(css, s2, { thinkingContent: current.activeSessionId === sid ? css.thinkingContent : current.thinkingContent })
      } else if (event.type === 'tool_start') {
        const autoApproved = isSub || event.auto_approved === true || event.risk_level === 'safe'
        if (isSub || event.risk_level !== 'safe' || event.visible === true) {
          css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
            isSub
              ? updateNode(m, aid!, (n) => nodeAddTool(n, { call_id: event.call_id, name: event.name, input: event.input as Record<string, unknown>, risk_level: event.risk_level, status: autoApproved ? 'running' : 'pending' }))
              : addToolBlock(m, { call_id: event.call_id, name: event.name, input: event.input as Record<string, unknown>, risk_level: event.risk_level, status: autoApproved ? 'running' : 'pending' })
          )
        }
        if (!isSub) {
          const edits = editsFromToolInput(event.name, event.input)
          if (edits && !autoApproved) {
            css.pendingBatchEdit = { call_id: event.call_id, tool_name: event.name, edits, risk_level: event.risk_level }
          } else {
            css.pendingPermission = !autoApproved
              ? { call_id: event.call_id, tool_name: event.name, params: event.input as Record<string, unknown>, risk_level: event.risk_level, always_allowable: event.always_allowable, categories: event.categories }
              : css.pendingPermission
          }
        }
        commit(css, s2)
      } else if (event.type === 'question') {
        // ask_user_question paused for a human answer — show the inline
        // QuestionCard. The tool block (tool_start) already renders; the answer
        // flows back via answerQuestion and the tool_result resolves both.
        css.pendingQuestion = { call_id: event.call_id, questions: event.questions }
        commit(css, s2)
      } else if (event.type === 'tool_result') {
        const patch = { status: event.is_error ? ('error' as const) : ('done' as const), result: event.output }
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          isSub
            ? updateNode(m, aid!, (n) => nodePatchTool(n, event.call_id, patch))
            : patchTool(m, event.call_id, patch)
        )
        if (!isSub) { css.pendingPermission = null; css.pendingBatchEdit = null; css.pendingQuestion = null }
        commit(css, s2)
      } else if (event.type === 'turn_diff') {
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) => ({
          ...m, turnDiff: { checkpoint_id: event.checkpoint_id, files: event.files },
        }))
        commit(css, s2)
      } else if (event.type === 'turn_start') {
        // Attach to the ORIGINAL user message (reused across retries).
        css.messages = mapAgentMsg(css.messages, userMsg.id, (m) => ({
          ...m, undo: { checkpoint_id: event.checkpoint_id, log_seq: event.log_seq },
        }))
        commit(css, s2)
      } else if (event.type === 'title_suggested') {
        commit(css, s2, {
          sessions: get().sessions.map((s) =>
            s.id === sid ? { ...s, title: event.title } : s
          ),
        })
      } else if (event.type === 'skill_used') {
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) => {
          const existing = m.skillsUsed || []
          if (existing.some((sk) => sk.slug === event.slug)) return m
          return { ...m, skillsUsed: [...existing, { name: event.name, slug: event.slug, source: event.source, trigger: event.trigger }] }
        })
        commit(css, s2)
      } else if (event.type === 'hook_result') {
        const run: HookRunEntry = {
          event: event.event,
          tool_name: event.tool_name,
          command: event.command,
          blocked: event.blocked,
          reason: event.reason,
          feedback_count: event.feedback.length,
          error: event.error,
          duration_ms: event.duration_ms,
        }
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) => ({
          ...m, hooksUsed: [...(m.hooksUsed || []), run],
        }))
        commit(css, s2)
      } else if (event.type === 'text_restart') {
        css.messages = mapAgentMsg(css.messages, agentMsgId, (m) =>
          isSub
            ? updateNode(m, aid!, (n) => ({ ...n, blocks: trimTrailingText(n.blocks), thinking: '' }))
            : { ...m, blocks: trimTrailingText(m.blocks || []), thinking: undefined }
        )
        css.streamingContent = ''
        css.thinkingContent = ''
        commit(css, s2)
      }
    })
    set({ _activeUnsub: unsub })

    // 5. Send the IPC — reuses the same user text.
    try {
      const result: any = await window.electronAPI.sendMessage(sid, userText, {
        modelId: ss.modelId,
        reasoningEffort: ss.reasoningEffort,
      })
      // Mirror the sendMessage finalize logic.
      const s3 = new Map(get().sessionStates)
      const sss = s3.get(sid)
      if (sss) {
        sss.messages = mapAgentMsg(sss.messages, agentMsgId, (m) => {
          const txt = blocksToText(m.blocks) || result?.text || ''
          const hasTools = (m.blocks || []).some((b) => b.type === 'tool')
          const blocks = (m.blocks && m.blocks.length > 0)
            ? m.blocks
            : txt ? [{ type: 'text' as const, text: txt }] : m.blocks
          const cacheUsage = result?.usage?.input_tokens
            ? { input_tokens: result.usage.input_tokens, cache_read: result.usage.cache_read || 0, cache_write: result.usage.cache_write || 0 }
            : undefined
          return { ...m, blocks, content: txt || (hasTools ? '' : '(no output)'), cacheUsage, timing: result?.timing, thinking: sss.thinkingContent || m.thinking }
        })
        sss.isStreaming = false
        sss.streamingContent = ''
        sss.thinkingContent = ''
        sss.hasUnread = get().activeSessionId !== sid
        window.electronAPI.saveDisplay(sid, sss.messages).catch(() => {})
      }
      const isActive = get().activeSessionId === sid
      const now = new Date().toISOString()
      const sortedSessions = get().sessions.map((s) =>
        s.id === sid ? { ...s, updated_at: now } : s
      ).sort((a, b) => {
        if (a.is_pinned !== b.is_pinned) return a.is_pinned ? -1 : 1
        return b.updated_at.localeCompare(a.updated_at)
      })
      set({
        sessionStates: s3,
        messages: isActive && sss ? sss.messages : get().messages,
        isStreaming: isActive ? false : get().isStreaming,
        streamingContent: isActive ? '' : get().streamingContent,
        thinkingContent: isActive ? '' : get().thinkingContent,
        sessions: sortedSessions,
      })
    } catch (e: any) {
      // Same as sendMessage: a crashed/killed process leaves a partial stream.
      // Discard it and replace the agent reply with a short error text.
      const s3 = new Map(get().sessionStates)
      const sss = s3.get(sid)
      if (sss) {
        sss.messages = mapAgentMsg(sss.messages, agentMsgId, (m) => ({
          ...m,
          blocks: [{ type: 'text', text: TURN_ERROR_TEXT() }],
          content: TURN_ERROR_TEXT(),
          thinking: undefined,
          subagents: undefined,
          turnDiff: undefined,
          skillsUsed: undefined,
          hooksUsed: undefined,
          cacheUsage: undefined,
          timing: undefined,
        }))
        sss.isStreaming = false
        sss.thinkingContent = ''
        sss.streamingContent = ''
        sss.hasUnread = get().activeSessionId !== sid
        window.electronAPI.saveDisplay(sid, sss.messages).catch(() => {})
      }
      set({ sessionStates: s3, isStreaming: false, error: 'An unexpected error occurred. Please try again.' })
    } finally {
      unsub()
      set({ _activeUnsub: null })
      get().refreshBridgeStatuses()
    }
  },

  consumeInputDraft: () => set({ inputDraft: null }),

  setDraft: (sessionId, text) => {
    const states = new Map(get().sessionStates)
    const ss = states.get(sessionId)
    if (ss) { ss.draftText = text }
    set({ sessionStates: states })
  },

  openDiff: async (checkpointId, label, path) => {
    const sid = get().activeSessionId
    if (!sid) return
    // Toggle: clicking the file that's already previewed closes the panel.
    // A different file (or checkpoint) switches to it instead of closing.
    const cur = get().diffView
    if (cur && cur.checkpointId === checkpointId && cur.activePath === path) {
      set({ diffView: null })
      return
    }
    // Already showing this checkpoint — just switch the active file, no refetch.
    if (cur && cur.checkpointId === checkpointId) {
      set({ diffView: { ...cur, activePath: path } })
      return
    }
    try {
      const files = await window.electronAPI.diffCheckpoint(sid, checkpointId)
      // Diff takes over the right dock; hide the other dock panels while shown.
      set({ diffView: { checkpointId, label, files, activePath: path }, selectedAgent: null, contextOpen: false })
    } catch (e: any) {
      set({ error: tGlobal('error.loadDiffFailed', { msg: e?.message }) })
    }
  },

  previewEditFiles: async (key, edits, activePath) => {
    const sid = get().activeSessionId
    if (!sid) return
    // Toggle: clicking the file already previewed closes the dock.
    const cur = get().diffView
    if (cur && cur.checkpointId === key && cur.activePath === activePath) {
      set({ diffView: null })
      return
    }
    // Same preview, different file — just switch (files already loaded).
    if (cur && cur.checkpointId === key) {
      set({ diffView: { ...cur, activePath } })
      return
    }
    // Group edits by file so a file edited multiple times shows one combined
    // before/after. Current disk content is the "after"; reverse each edit
    // (new_string → old_string, last-applied first) to reconstruct the "before".
    const byPath = new Map<string, { old_string: string; new_string: string }[]>()
    for (const e of edits) {
      const list = byPath.get(e.path) || []
      list.push({ old_string: e.old_string, new_string: e.new_string })
      byPath.set(e.path, list)
    }
    try {
      const files: CheckpointFileDiff[] = []
      for (const [path, group] of byPath) {
        const after = (await window.electronAPI.readFile(sid, path)) ?? ''
        let before = after
        for (let k = group.length - 1; k >= 0; k--) {
          const g = group[k]
          const nn = (g.new_string || '').replace(/\r\n/g, '\n')
          const on = (g.old_string || '').replace(/\r\n/g, '\n')
          const idx = before.indexOf(nn)
          if (idx !== -1) before = before.slice(0, idx) + on + before.slice(idx + nn.length)
        }
        // Empty "before" with non-empty "after" is a newly created file (e.g.
        // write_file), so label it added rather than modified.
        const status = before === '' && after !== '' ? 'A' : 'M'
        files.push({ path, status, old_content: before, new_content: after })
      }
      set({
        diffView: { checkpointId: key, label: 'Edit preview', files, activePath },
        selectedAgent: null,
        contextOpen: false,
      })
    } catch (e: any) {
      set({ error: tGlobal('error.previewEditFailed', { msg: e?.message }) })
    }
  },

  // Switch which file the open diff panel shows (chip click) — no refetch,
  // no toggle-close, just changes the active file.
  setDiffActive: (path) => {
    const cur = get().diffView
    if (cur) set({ diffView: { ...cur, activePath: path } })
  },

  closeDiff: () => set({ diffView: null }),

  openContextMenu: (menu: { x: number; y: number; selection: string; markdown: string; target?: { type: 'session' | 'group'; id: string; groupId?: string | null; provider?: string; model?: string; isPinned?: boolean } }) =>
    set({ contextMenu: { x: menu.x, y: menu.y, selection: menu.selection, markdown: menu.markdown }, contextMenuTarget: menu.target || null }),
  closeContextMenu: () => set({ contextMenu: null, contextMenuTarget: null }),
  startEditSession: (id) => set({ editingSessionId: id, editingGroupId: null }),
  cancelEditSession: () => set({ editingSessionId: null }),
  startEditGroup: (id) => set({ editingGroupId: id, editingSessionId: null }),
  cancelEditGroup: () => set({ editingGroupId: null }),

  showSkills: async () => {
    set({ mainView: 'skills' })
    // Scan using the active session's cwd (for project skills), else the app cwd.
    const sid = get().activeSessionId
    const cwd = get().sessions.find((s) => s.id === sid)?.cwd || get().workingDir
    try {
      const skills = await window.electronAPI.listSkills(cwd)
      set({ skills })
      // Auto-select the first skill so the preview isn't blank.
      if (skills.length > 0 && !get().selectedSkillPath) {
        await get().selectSkill(skills[0].path)
      }
    } catch (e: any) {
      set({ error: tGlobal('error.listSkillsFailed', { msg: e?.message }) })
    }
  },

  // Lightweight skills loader for chat autocomplete — doesn't switch views.
  loadSkills: async () => {
    const sid = get().activeSessionId
    const cwd = get().sessions.find((s) => s.id === sid)?.cwd || get().workingDir
    try {
      const skills = await window.electronAPI.listSkills(cwd)
      set({ skills })
    } catch { /* silent — skills stay stale rather than erroring in chat */ }
  },

  showChat: () => set({ mainView: 'chat' }),

  showSettings: () => set({ mainView: 'settings' }),

  selectSkill: async (path) => {
    set({ selectedSkillPath: path })
    try {
      const content = await window.electronAPI.readSkill(path)
      // Guard against an out-of-order response: only apply if still selected.
      if (get().selectedSkillPath === path) set({ skillContent: content })
    } catch (e: any) {
      set({ error: tGlobal('error.readSkillFailed', { msg: e?.message }) })
    }
  },

  // Toggle `disabled` on a skill in <cwd>/.cluxmate/skills.json. Optimistic
  // local update; takes effect next session (no hot-swap of system prompt).
  setSkillDisabled: async (slug, disabled) => {
    const sid = get().activeSessionId
    const cwd = get().sessions.find((s) => s.id === sid)?.cwd || get().workingDir
    if (!cwd) return

    // Normalize path separators so the suffix match works on Windows too
    // (path.join produces backslashes, but we use forward slashes in the pattern).
    const suffix = `/${slug}/SKILL.md`
    const matchPath = (sk: SkillMeta) =>
      (sk.path || '').replace(/\\/g, '/').endsWith(suffix)

    // Optimistic local update.
    set({
      skills: get().skills.map((sk) =>
        matchPath(sk) ? { ...sk, disabled } : sk
      ),
    })
    try {
      await window.electronAPI.setSkillDisabled(cwd, slug, disabled)
    } catch (e: any) {
      // Revert on failure.
      set({
        skills: get().skills.map((sk) =>
          matchPath(sk) ? { ...sk, disabled: !disabled } : sk
        ),
        error: tGlobal('error.toggleSkillFailed', { msg: e?.message }),
      })
    }
  },

  showMcp: async () => {
    set({ mainView: 'mcp', mcpLoading: true })
    const sid = get().activeSessionId
    if (!sid) {
      set({ mcpServers: [], mcpLoading: false })
      return
    }
    try {
      // Race the fetch against a hard timeout. listMcp triggers a cold Python
      // spawn + initialize (imports + MCP handshake), which can run tens of
      // seconds and, if the bridge spawn wedges, may never settle at all. The
      // finally below only clears the spinner when the promise resolves/rejects
      // — so without this race a wedged bridge leaves "Loading..." spinning forever.
      const TIMEOUT_MS = 45000
      const servers = await Promise.race([
        window.electronAPI.listMcp(sid),
        new Promise<never>((_, rej) =>
          setTimeout(() => rej(new Error(`Timeout (${TIMEOUT_MS / 1000}s) — first MCP server start (e.g. npx cold cache) can be slow, try again later`)), TIMEOUT_MS)
        ),
      ])
      // Guard against out-of-order response (e.g. session switched mid-fetch):
      // only APPLY the data when it still belongs to the current session+view.
      if (get().activeSessionId === sid && get().mainView === 'mcp') {
        set({ mcpServers: servers })
        // Auto-select the first server so the right pane isn't blank.
        if (servers.length > 0 && !get().selectedMcpServer) {
          set({ selectedMcpServer: servers[0].name })
        }
      }
    } catch (e: any) {
      if (get().activeSessionId === sid && get().mainView === 'mcp') {
        set({ error: tGlobal('error.listMcpFailed', { msg: e?.message }) })
      }
    } finally {
      // Clear the spinner whenever this fetch still owns the current session.
      // The original bug gated the clear on `mainView === 'mcp'` too, so if the
      // view flipped during the ~8s initialize+list round trip the flag was
      // never reset — permanent "Loading...". Gating on session only also avoids a
      // stale fetch from an old session clobbering a newer fetch's spinner.
      if (get().activeSessionId === sid) set({ mcpLoading: false })
    }
  },

  selectMcpServer: (name) => set({ selectedMcpServer: name }),

  setMcpDisabled: async (name, disabled) => {
    const sid = get().activeSessionId
    if (!sid) return
    // Optimistic update so the toggle feels responsive.
    set({
      mcpServers: get().mcpServers.map((s) =>
        s.name === name ? { ...s, disabled } : s
      ),
    })
    try {
      await window.electronAPI.setMcpDisabled(sid, name, disabled)
      // Do NOT refetch here. listMcp reads the RUNNING bridge's status, which
      // is cached from initialize and never re-reads mcp.json — so a refetch
      // returns the pre-toggle `disabled` value and clobbers the optimistic
      // update, making the switch snap back ("won't toggle off"). The write to
      // mcp.json succeeded; the toggle takes effect on the next session/
      // initialize (no hot-swap). Keep the optimistic state as the truth.
    } catch (e: any) {
      // Revert optimistic update on failure.
      set({
        mcpServers: get().mcpServers.map((s) =>
          s.name === name ? { ...s, disabled: !disabled } : s
        ),
        error: tGlobal('error.toggleMcpFailed', { msg: e?.message }),
      })
    }
  },

  showHooks: async () => {
    set({ mainView: 'hooks', hooksLoading: true })
    const sid = get().activeSessionId
    if (!sid) {
      set({ hooks: [], hooksLoading: false })
      return
    }
    try {
      // getHooks reads the running bridge's already-loaded settings.json view
      // (fast, no cold spawn). A cold bridge returns { hooks: [] } from the
      // main process, so the empty state doubles as "warming up / none configured".
      const cfg = await window.electronAPI.getHooks(sid)
      if (get().activeSessionId === sid && get().mainView === 'hooks') {
        set({ hooks: cfg.hooks })
      }
    } catch (e: any) {
      if (get().activeSessionId === sid && get().mainView === 'hooks') {
        set({ error: tGlobal('error.listHooksFailed', { msg: e?.message }) })
      }
    } finally {
      if (get().activeSessionId === sid) set({ hooksLoading: false })
    }
  },

  reloadHooks: async () => {
    set({ hooksLoading: true })
    const sid = get().activeSessionId
    if (!sid) {
      set({ hooksLoading: false })
      return
    }
    try {
      // Re-read settings.json in place on the running Python agent (no restart)
      // and reflect the refreshed list. reloadHooks returns the new list directly.
      const cfg = await window.electronAPI.reloadHooks(sid)
      if (get().activeSessionId === sid) set({ hooks: cfg.hooks })
    } catch (e: any) {
      if (get().activeSessionId === sid) {
        set({ error: tGlobal('error.listHooksFailed', { msg: e?.message }) })
      }
    } finally {
      if (get().activeSessionId === sid) set({ hooksLoading: false })
    }
  },

  // Fire-and-forget Notification trigger (test/demo button in HooksView):
  // runs the session's Notification hooks with `message` in the payload.
  notifyHooks: async (message: string) => {
    const sid = get().activeSessionId
    if (!sid) return
    try {
      await window.electronAPI.notifyHooks(sid, message)
    } catch (e: any) {
      set({ error: tGlobal('error.notifyHooksFailed', { msg: e?.message }) })
    }
  },

  clearError: () => set({ error: null }),

  setError: (msg) => set({ error: msg }),
}))

// Real-time bridge state pushes from the main process. Two directions:
//   - running=false: the idle reaper killed a long-idle process, or a process
//     exited/crashed — grey the dot immediately instead of waiting for the next
//     user-triggered refresh.
//   - running=true: a background pre-warm spawn finished its initialize
//     handshake and is now live — light the dot up on session switch without
//     waiting for the user to send a message. (refreshBridgeStatuses runs right
//     after switchSession, before the ~1.7s handshake resolves, so it always
//     saw isRunning=false; this push closes that gap.)
// Absent `running` is treated as false for backward compatibility.
window.electronAPI.onBridgeStatusChanged(({ sessionIds, running }) => {
  const bridgeStatuses = { ...useStore.getState().bridgeStatuses }
  for (const sid of sessionIds) bridgeStatuses[sid] = running ?? false
  useStore.setState({ bridgeStatuses })
})
