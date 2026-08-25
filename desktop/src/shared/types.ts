// A configurable model entry in ~/.cluxmate/config.json. `provider` is a
// free-text vendor label; `api_type` selects the API family (currently
// OpenAI-style; `context_1m` is metadata only (displayed, not yet acted on).
// `reasoning_efforts` (raw provider enum values) overrides the per-dialect
// system preset (see shared/reasoning.ts); empty = use the preset. There is
// no per-model default — every model starts on "default" (no reasoning fields).
export interface ModelEntry {
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

export interface GroupMeta {
  id: string
  name: string
  created_at: string
  sort_order: number
  is_auto: boolean
  // Resolved working directory for auto groups (null for manual groups). Two
  // directories sharing a basename are distinct projects keyed by this path.
  path?: string | null
}

export interface SessionMeta {
  id: string
  title: string
  // provider = vendor label, model = model_name — both denormalized for display.
  provider: string
  model: string
  // The config entry id this session was created with (pins the model to
  // rebuild the provider from). May be null on rows created before this schema.
  model_id: string | null
  api_type: string | null
  // The session's selected reasoning level (null → provider default).
  reasoning_effort: string | null
  cwd: string
  created_at: string
  updated_at: string
  message_count: number
  group_id: string | null
  is_pinned: boolean
}

// One search hit across session metadata + message bodies. `meta` is the full
// session row (so the results list can reuse SessionItem directly); `snippets`
// are the matched message-body fragments (max a handful), with the keyword
// highlighted by the renderer. A hit on title/cwd/provider/model has an empty
// `snippets` array — those fields are already visible in the row itself.
export interface SessionSearchHit {
  meta: SessionMeta
  // Body fragments where the query matched, capped by the backend. Empty when
  // the match came only from metadata (title/cwd/provider/model).
  snippets: string[]
}

export interface CreateSessionParams {
  title?: string
  cwd: string
  modelId: string
  apiType: string
  // Denormalized snapshot for display.
  provider: string
  model: string
}

// Per-message model + reasoning-effort override carried on chat/send.
export interface ChatSendOptions {
  modelId: string
  reasoningEffort: string | null
}

export type RiskLevel = 'safe' | 'write' | 'dangerous'

// agent_id tags which agent in the tree emitted the event. "root" (or absent,
// for older streams) is the top-level agent; any other id is a subagent node.
export interface ToolStartEvent {
  type: 'tool_start'
  call_id: string
  name: string
  input: Record<string, unknown>
  risk_level: RiskLevel
  // true when the server auto-approved (safe risk, or "always approve"
  // previously granted for this tool) — no permission prompt is shown.
  auto_approved?: boolean
  // true when a safe (auto-approved, normally hidden) tool should still render
  // a running card in the root UI — e.g. web_search/web_fetch, so a multi-minute
  // network call shows progress instead of an empty spinner.
  visible?: boolean
  agent_id?: string
}

export interface ToolResultEvent {
  type: 'tool_result'
  call_id: string
  output: string
  is_error: boolean
  agent_id?: string
}

export interface TextDeltaEvent {
  type: 'text_delta'
  content: string
  agent_id?: string
}

export interface ThinkingEvent {
  type: 'thinking'
  content: string
  agent_id?: string
}

// A single choice in an ask_user_question prompt.
export interface QuestionOption {
  label: string
  description?: string
}

// One question the model asks the user via the ask_user_question tool.
export interface Question {
  id: string
  question: string
  header?: string
  options?: QuestionOption[]
  multi_select?: boolean
}

// One answered question, sent back to the Python bridge via question/answer.
export interface QuestionAnswer {
  id: string
  selected: string[]
  custom?: string
}

// Emitted by the Python bridge when the ask_user_question tool pauses for a
// human answer. Drives the inline QuestionCard; the answer flows back through
// the question/answer RPC and the tool result is the model-visible reply.
export interface QuestionEvent {
  type: 'question'
  call_id: string
  questions: Question[]
  agent_id?: string
}

// Emitted when a subagent is spawned / finishes. Drives the subagent tree.
export interface AgentStartEvent {
  type: 'agent_start'
  agent_id: string
  parent_id: string
  subagent_type: string
  description: string
  depth: number
  // The task prompt the subagent was launched with (shown in the inspector,
  // visually distinct from the agent's answer).
  prompt?: string
}

export interface AgentEndEvent {
  type: 'agent_end'
  agent_id: string
  status: 'done' | 'error'
  result: string
  // Cumulative token usage of this subagent across all its own LLM calls,
  // reported by the Python bridge once the child finishes. Used to fold
  // subagent usage into the session's input/output token totals.
  input_tokens?: number
  output_tokens?: number
}

// One file changed during a turn — lightweight (no content), for the inline
// "changed files" card. Content is fetched lazily via diffCheckpoint on click.
export interface TurnFileChange {
  path: string
  status: 'A' | 'M' | 'D'
  additions: number
  deletions: number
}

// Emitted once after a turn completes: the files that turn changed, plus the
// checkpoint id whose diff() has the full before/after for each file.
export interface TurnDiffEvent {
  type: 'turn_diff'
  agent_id?: string
  checkpoint_id: string
  files: TurnFileChange[]
}

// Emitted at the start of a turn (after the pre-turn snapshot). Carries the
// undo anchor for the user message that triggered this turn: the pre-turn
// checkpoint sha (files rewind target) and the pre-turn session-log seq
// (JSONL truncation point). Only emitted when checkpoints are available.
export interface TurnStartEvent {
  type: 'turn_start'
  agent_id?: string
  checkpoint_id: string
  log_seq: number
}

// Emitted when a skill is invoked — via a /slug command (trigger 'command') or
// the model's use_skill tool (trigger 'auto'). Drives the inline annotation.
export interface SkillUsedEvent {
  type: 'skill_used'
  name: string
  slug: string
  source: 'global' | 'project'
  trigger: 'command' | 'auto'
  agent_id?: string
}

// Emitted once, after the first turn completes, with an LLM-generated session
// title that replaces the first-line default in the sidebar.
export interface TitleSuggestedEvent {
  type: 'title_suggested'
  title: string
}

// A lifecycle hook (settings.json) is about to run. `event` is the hook event
// (PreToolUse / PostToolUse / UserPromptSubmit / Stop), `tool_name` the tool it
// matched against (null for tool-less events), `command` the shell command.
export interface HookStartEvent {
  type: 'hook_start'
  event: string
  tool_name: string | null
  command: string
}

// A lifecycle hook finished. `blocked`/`reason` report a blocking decision;
// `feedback` carries injected additionalContext; `exit_code`/`error`/`duration_ms`
// are execution metadata (error non-null when the command failed to run).
export interface HookResultEvent {
  type: 'hook_result'
  event: string
  tool_name: string | null
  command: string
  blocked: boolean
  reason: string
  feedback: string[]
  exit_code: number | null
  error: string | null
  duration_ms: number
}

// One completed hook run, attached to an agent reply as a faint annotation
// (mirrors skillsUsed). Derived from HookResultEvent; persisted with the display
// transcript so the annotation survives reopening.
export interface HookRunEntry {
  event: string
  tool_name: string | null
  command: string
  blocked: boolean
  reason: string
  feedback_count: number
  error: string | null
  duration_ms: number
}

// Emitted when a generation attempt is discarded and restarted (a Stop hook
// blocked the reply). The UI clears the previously-streamed text/thinking so the
// new stream starts fresh instead of appending to the rejected attempt.
export interface TextRestartEvent {
  type: 'text_restart'
  agent_id?: string
}

export type StreamEvent =
  | ToolStartEvent | ToolResultEvent | TextDeltaEvent | ThinkingEvent
  | AgentStartEvent | AgentEndEvent | TurnDiffEvent | TurnStartEvent | SkillUsedEvent
  | TitleSuggestedEvent | QuestionEvent
  | HookStartEvent | HookResultEvent | TextRestartEvent

export type SessionStreamEvent = StreamEvent & {
  sessionId: string
}

export interface ChatResult {
  stop_reason: string
  text: string | null
  // null when stop_reason is 'cancelled' or 'timeout' — do not overwrite
  // the session file in those cases (would wipe prior history).
  history: Record<string, unknown>[] | null
  usage?: {
    input_tokens: number
    cache_read: number
    cache_write: number
  }
  // Model-generation timing for the turn (approval + tool execution excluded).
  timing?: {
    ttft_ms: number | null
    gen_ms: number
    out_tokens: number
  }
}

export interface PermissionRequest {
  call_id: string
  tool_name: string
  params: Record<string, unknown>
  risk_level: RiskLevel
}

// A pending ask_user_question prompt shown by the QuestionCard.
export interface PendingQuestion {
  call_id: string
  questions: Question[]
}

// Development mode — the InputBox cycles through these in order.
// - plan:        read-only. The agent has no write tools at all (hard isolation).
// - default:     write/dangerous prompt for approval.
// - acceptEdits: writes auto-approve; dangerous still prompts.
// - yolo:        everything auto-approves, INCLUDING dangerous (rm -rf, delete).
export type PermissionMode = 'plan' | 'default' | 'acceptEdits' | 'yolo'

// Per-session tool-approval policy. `mode` is per-session (not persisted);
// always_allow_tools is persisted per-project. accept_edits is a derived
// backward-compat field (true iff mode === 'acceptEdits').
export interface Permissions {
  mode: PermissionMode
  accept_edits: boolean
  always_allow_tools: string[]
}

// One active lifecycle hook, normalized from settings.json (global + project).
// `matcher` is the tool-name regex (null = matches every tool, ignored for
// tool-less events); `command` is the shell command; `timeout` the per-hook
// wall-clock bound in seconds.
export interface HookEntry {
  event: string
  matcher: string | null
  command: string
  timeout: number
}

export interface HooksConfig {
  hooks: HookEntry[]
}

// Which settings.json to open/edit: the per-user global one or the active
// session's project one. Both are read by the agent (project runs after global).
export type HooksScope = 'global' | 'project'

export interface BatchEditRequest {
  call_id: string
  tool_name: string
  edits: { path: string; old_string: string; new_string: string }[]
  risk_level: RiskLevel
}

// An agent turn is an ordered sequence of text and tool blocks so the UI can
// render them interleaved in the exact order the agent produced them (text,
// then a tool, then more text, ...) rather than lumping all tools at the end.
export interface TextBlock {
  type: 'text'
  text: string
}

export interface ToolBlock {
  type: 'tool'
  tool: ToolCallEntry
}

export type MessageBlock = TextBlock | ToolBlock

// A subagent node in the tree hung off an agent message. Its `blocks` mirror
// the root message's block model (interleaved text + tool cards) so the
// inspector panel can reuse the same renderers.
export interface AgentNode {
  agent_id: string
  parent_id: string
  subagent_type: string
  description: string
  depth: number
  status: 'running' | 'done' | 'error'
  blocks: MessageBlock[]
  result?: string
  // The prompt this subagent was launched with.
  prompt?: string
  // Reasoning/thinking content streamed during this subagent's execution.
  thinking?: string
  // Cumulative token usage across this subagent's own LLM calls, set from the
  // agent_end event (live) and reconstructed from the child JSONL on reload.
  input_tokens?: number
  output_tokens?: number
}

// A subagent node reconstructed from the authoritative Python JSONL (see the
// session/replay RPC). Identical to AgentNode plus `turn` — the parent-session
// turn whose agent message owns the whole subtree — used to re-attach the tree
// on reload.
export interface ReplaySubagent extends AgentNode {
  turn: number
}

export interface ChatMessage {
  id: string
  role: 'user' | 'agent' | 'system'
  content: string
  // Ordered blocks for agent messages. When present, the renderer uses these
  // instead of `content`/`tool_calls`. `content` is kept as a flat text
  // fallback (e.g. for the final result or older messages without blocks).
  blocks?: MessageBlock[]
  tool_calls?: ToolCallEntry[]
  tool_results?: ToolResultEntry[]
  // Subagent nodes keyed by agent_id, reassembled from the flat event stream.
  // Serialized with the display transcript so reopening a session shows the
  // same tree.
  subagents?: Record<string, AgentNode>
  // Files this turn changed + the checkpoint id to diff against. Drives the
  // inline "changed files" card; persisted with the display transcript.
  turnDiff?: { checkpoint_id: string; files: TurnFileChange[] }
  // Undo anchor for a user message: the pre-turn checkpoint sha (files rewind
  // target) + the session-log seq before this turn (JSONL truncation point).
  // Present only on user messages sent while checkpoints were available; drives
  // the per-message undo button. Persisted with the display transcript.
  undo?: { checkpoint_id: string; log_seq: number }
  // Skills invoked during this turn (slug-deduped). Rendered as an italic,
  // faint annotation under the reply; persisted with the display transcript.
  skillsUsed?: { name: string; slug: string; source: 'global' | 'project'; trigger: 'command' | 'auto' }[]
  // Lifecycle hooks that ran during this turn. Rendered as a faint annotation
  // under the reply (like skillsUsed); persisted with the display transcript.
  hooksUsed?: HookRunEntry[]
  // Per-turn prompt-cache usage. Present only when the provider reports cache
  // tokens. Drives the inline cache-hit badge under agent replies.
  cacheUsage?: {
    input_tokens: number
    cache_read: number
    cache_write: number
  }
  // Model-generation timing for this reply (approval + tool execution
  // excluded). Persisted with the display transcript so it survives reopening
  // the session; drives the footer's first-token latency / token-rate readout.
  timing?: {
    ttft_ms: number | null
    gen_ms: number
    out_tokens: number
  }
  thinking?: string
  timestamp: number
}

export interface ToolCallEntry {
  call_id: string
  name: string
  input: Record<string, unknown>
  risk_level: RiskLevel
  status: 'pending' | 'running' | 'denied' | 'done' | 'error'
  result?: string
}

export interface ToolResultEntry {
  call_id: string
  output: string
  is_error: boolean
}

// A shadow-git workspace snapshot. `id` is the commit sha; `timestamp` is ISO
// (falls back to unix seconds when the metadata line lacks one).
export interface Checkpoint {
  id: string
  label: string
  timestamp: string
  files_changed: number
}

// One file's before/after for the diff viewer. status: A(dded) / M(odified) /
// D(eleted). Content is empty string for the absent side.
export interface CheckpointFileDiff {
  path: string
  status: 'A' | 'M' | 'D'
  old_content: string
  new_content: string
}

export interface RestoreResult {
  restored: string[]
  deleted: string[]
  // Files another session also changed after the target checkpoint. The rewind
  // still applies this session's version, but these were clobbered — surface a
  // warning so the user knows another session's edits to them were overwritten.
  conflicts?: string[]
}

// An installed skill discovered from a skills directory. `source` marks where
// it came from: global (~/.cluxmate/skills) or project (cwd/.cluxmate/skills).
// `path` is the absolute path to its SKILL.md, used as the read key + list id.
export interface SkillMeta {
  name: string
  description: string
  source: 'global' | 'project'
  path: string
  disabled: boolean
}

// MCP server transport: stdio (local subprocess) or http (remote).
// Derived from the config shape — has `command` → local; has `url` → remote.
export type McpTransport = 'local' | 'remote'

// Live status of an MCP server connection, surfaced from the per-session
// Python process via the mcp/list JSON-RPC method.
export type McpServerStatus =
  | 'connected'      // handshake ok, tools loaded
  | 'failed'        // spawn or handshake failed; see `error`
  | 'disabled'      // config has disabled: true — skipped at load
  | 'disconnected'  // not yet loaded or after shutdown

export interface McpTool {
  name: string
  description: string
  // Anthropic-style JSON schema; passed through to the provider as-is.
  input_schema: Record<string, unknown>
}

export interface McpServer {
  name: string
  transport: McpTransport
  status: McpServerStatus
  disabled: boolean
  // Populated when status is 'failed' — the handshake / spawn error message.
  error?: string | null
  tools: McpTool[]
}

// Per-session Python process liveness, reported by the main process's bridges Map.
export interface BridgeStatus {
  sessionId: string
  running: boolean
}

// Git state for a working directory, surfaced from the main process (which runs
// git directly, independent of the Python agent). currentBranch is the branch
// name, or a short commit sha on a detached HEAD, or null for an unborn branch.
export interface GitInfo {
  inRepo: boolean
  currentBranch: string | null
  hasChanges: boolean
}

export interface GitBranchList {
  current: string | null
  branches: string[]
  hasChanges: boolean
}

// How to reconcile uncommitted changes before switching branches.
export type GitCheckoutStrategy = 'direct' | 'stash' | 'commit' | 'discard'

export interface GitCheckoutResult {
  ok: boolean
  branch?: string
  message?: string
}

// A compaction summary present in a step's reconstructed context. `index` is its
// position in `TurnStep.messages`; `turn`/`step` identify which step did the
// compaction; `shadowed` are the messages that were replaced (no longer
// model-visible, but still recoverable from the raw log).
export interface TurnCompaction {
  index: number
  turn: number
  step: number | null
  shadowed: Record<string, unknown>[]
}

// One LLM call's exact input within a turn (a turn can have multiple steps when
// the agent runs tools between calls).
export interface TurnStep {
  step: number
  messages: Record<string, unknown>[]
  // Parallel to `messages`: origin of each user message ("human" | "memory" |
  // "skill" | "mode" | "compaction" | "interruption"), or null for assistant/tool.
  sources: (string | null)[]
  compactions: TurnCompaction[]
  tokens_estimate: number
}

// A reconstructed per-turn context: the constant request envelope (system +
// tools + config) plus every step's exact model input, rebuilt from the JSONL log.
// The raw request for a step is [{"role":"system","content":system}] + messages.
export interface TurnContext {
  turn: number
  system: string | null
  tools: Record<string, unknown>[]
  config: Record<string, unknown>
  steps: TurnStep[]
}

declare global {
  interface Window {
    electronAPI: ElectronAPI
  }
}

export interface ElectronAPI {
  // --- projects ---
  createGroup: (name: string) => Promise<GroupMeta>
  renameGroup: (id: string, name: string) => Promise<void>
  deleteGroup: (id: string) => Promise<void>
  moveSession: (sessionId: string, groupId: string | null) => Promise<void>
  moveSessionToProject: (sessionId: string) => Promise<void>
  renameSession: (id: string, title: string) => Promise<void>
  pinSession: (id: string, pinned: boolean) => Promise<void>
  listGroups: () => Promise<GroupMeta[]>

  // Bridge process liveness
  getBridgeStatuses: (sessionIds: string[]) => Promise<BridgeStatus[]>

  listSessions: () => Promise<SessionMeta[]>
  // Full-text search over session metadata + message bodies (display
  // transcripts on disk). Returns [] for an empty/whitespace query.
  searchSessions: (query: string) => Promise<SessionSearchHit[]>
  createSession: (params: CreateSessionParams) => Promise<SessionMeta>
  deleteSession: (id: string) => Promise<void>
  switchSession: (id: string) => Promise<{ history: Record<string, unknown>[]; display: ChatMessage[] } | null>
  updateSessionCwd: (id: string, cwd: string) => Promise<void>
  // Persist the session's selected model + reasoning level (denormalized
  // provider/model/api_type too, so the sidebar label stays in sync).
  setSessionModel: (id: string, modelId: string, reasoningEffort: string | null) => Promise<void>
  saveDisplay: (id: string, messages: ChatMessage[]) => Promise<void>
  truncateHistory: (id: string, seq: number) => Promise<void>

  sendMessage: (sessionId: string, text: string, options?: ChatSendOptions) => Promise<ChatResult>
  cancelChat: (sessionId: string) => Promise<void>

  approveTool: (sessionId: string, callId: string, always?: boolean, selected?: number[]) => Promise<void>
  denyTool: (sessionId: string, callId: string) => Promise<void>
  answerQuestion: (sessionId: string, callId: string, answers: QuestionAnswer[]) => Promise<void>

  // Tool-approval policy for the given session's bridge. getPermissions reads
  // the current state; setMode switches the development mode (plan/default/
  // acceptEdits/yolo) — plan changes the toolset, so the Python side rebuilds
  // the agent. Mode is per-session and not persisted.
  getPermissions: (sessionId: string) => Promise<Permissions>
  setMode: (sessionId: string, mode: PermissionMode) => Promise<Permissions>

  // Lifecycle hooks (settings.json) active in the given session's project. Read
  // from the per-session Python process (which merged global + project config);
  // returns an empty list while the bridge is still warming up.
  getHooks: (sessionId: string) => Promise<HooksConfig>
  // Re-read settings.json in place (no session restart) and return the new list.
  reloadHooks: (sessionId: string) => Promise<HooksConfig>
  // Open a hooks settings.json in the user's editor: `global` → ~/.cluxmate/
  // settings.json, `project` → <session cwd>/.cluxmate/settings.json. Creates the
  // file (with an empty {"hooks":{}} skeleton) if it doesn't exist, then opens it.
  openHooksSettings: (sessionId: string, scope: HooksScope) => Promise<void>

  // Writable-folder grants (sandbox-grants.json) — user-global. get reads the
  // current set; set replaces it (revoked folders are restored Low → Medium on
  // Windows). Returns { paths, restored }.
  getSandboxGrants: () => Promise<{ paths: string[] }>
  setSandboxGrants: (paths: string[]) => Promise<{ paths: string[]; restored: string[] }>

  listCheckpoints: (sessionId: string) => Promise<Checkpoint[]>
  diffCheckpoint: (sessionId: string, checkpointId: string) => Promise<CheckpointFileDiff[]>
  restoreCheckpoint: (sessionId: string, checkpointId: string) => Promise<RestoreResult>
  // Reconstruct the session's subagent tree from the authoritative Python JSONL.
  // Returns an empty list when the session spawned no subagents or the bridge is
  // still warming up (fire-and-forget on switchSession to fill a missing tree).
  replaySession: (sessionId: string) => Promise<{ subagents: ReplaySubagent[] }>
  // Reconstruct every turn's exact first-request context from the JSONL log.
  // `sessionId` is the parent session (whose bridge serves the RPC);
  // `targetSessionId` is whose log to reconstruct — pass a subagent id to
  // inspect a child's context, or omit for the parent.
  getTurnContexts: (sessionId: string, targetSessionId?: string) => Promise<{ turns: TurnContext[] }>

  writeClipboard: (text: string) => Promise<void>

  listSkills: (cwd: string) => Promise<SkillMeta[]>
  readSkill: (path: string) => Promise<string>
  // Toggle `disabled` on a skill in <cwd>/.cluxmate/skills.json. Takes effect
  // on the next `initialize` (next session or explicit reload) — the running
  // session's skill list is NOT hot-swapped.
  setSkillDisabled: (cwd: string, slug: string, disabled: boolean) => Promise<void>

  // Read a workspace file (for the inline edit-diff preview). Resolves inside
  // the session cwd; returns null if the file is missing.
  readFile: (sessionId: string, path: string) => Promise<string | null>

  getGitInfo: (cwd: string) => Promise<GitInfo>
  listGitBranches: (cwd: string) => Promise<GitBranchList>
  checkoutBranch: (cwd: string, branch: string, strategy: GitCheckoutStrategy) => Promise<GitCheckoutResult>

  // Fetch live MCP server status + tool list from the per-session Python
  // process (which owns the MCP client connections). Returns an empty list
  // while the bridge is still warming up — caller should retry or show a
  // loading state.
  listMcp: (sessionId: string) => Promise<McpServer[]>
  // Toggle `disabled` on a server in <cwd>/.cluxmate/mcp.json. Takes effect
  // on the next `initialize` (next session or explicit reload) — the running
  // session's tool list is NOT hot-swapped.
  setMcpDisabled: (sessionId: string, name: string, disabled: boolean) => Promise<void>

  getVersion: () => Promise<string>
  getDefaultCwd: () => Promise<string>
  getModelsConfig: () => Promise<{ models: ModelEntry[]; activeId: string }>
  saveModelsConfig: (cfg: { models: ModelEntry[]; activeId: string }) => Promise<void>
  // Update just config.json's active_model_id (the Settings "default model")
  // without rewriting the whole model list or restarting the bridge.
  setDefaultModel: (modelId: string) => Promise<void>
  selectDirectory: () => Promise<string | null>
  openExternal: (path: string) => Promise<void>

  onStreamEvent: (callback: (event: SessionStreamEvent) => void) => () => void
  // `running` is the sessions' new bridge state: true when a background spawn
  // just came online, false when a process exited / was reaped. Absent is
  // treated as false (offline) for backward compatibility.
  onBridgeStatusChanged: (callback: (payload: { sessionIds: string[]; running?: boolean }) => void) => () => void

  // Custom (frameless) title bar window controls.
  minimizeWindow: () => Promise<void>
  toggleMaximizeWindow: () => Promise<void>
  closeWindow: () => Promise<void>
  isWindowMaximized: () => Promise<boolean>
  onWindowMaximizedChanged: (callback: (maximized: boolean) => void) => () => void
}
