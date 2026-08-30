import { contextBridge, ipcRenderer } from 'electron'
import { IPC } from '../shared/ipc-channels'
import type {
  ElectronAPI, CreateSessionParams,
  ChatResult, SessionStreamEvent, SsrConfigPayload,
} from '../shared/types'

const api: ElectronAPI = {
  listSessions: () => ipcRenderer.invoke(IPC.SESSION_LIST),
  searchSessions: (query: string) => ipcRenderer.invoke(IPC.SESSION_SEARCH, query),
  createSession: (params: CreateSessionParams) => ipcRenderer.invoke(IPC.SESSION_CREATE, params),
  deleteSession: (id: string) => ipcRenderer.invoke(IPC.SESSION_DELETE, id),
  switchSession: (id: string) => ipcRenderer.invoke(IPC.SESSION_SWITCH, id),
  updateSessionCwd: (id: string, cwd: string) => ipcRenderer.invoke(IPC.SESSION_UPDATE_CWD, id, cwd),
  setSessionModel: (id: string, modelId: string, reasoningEffort: string | null) =>
    ipcRenderer.invoke(IPC.SESSION_SET_MODEL, id, modelId, reasoningEffort),
  saveDisplay: (id: string, messages) => ipcRenderer.invoke(IPC.SESSION_SAVE_DISPLAY, id, messages),
  truncateHistory: (id: string, len: number) => ipcRenderer.invoke(IPC.SESSION_TRUNCATE, id, len),
  replaySession: (id: string) => ipcRenderer.invoke(IPC.SESSION_REPLAY, id),
  getTurnContexts: (id: string, targetId?: string) => ipcRenderer.invoke(IPC.SESSION_CONTEXT, id, targetId),

  sendMessage: (sessionId: string, text: string, options) =>
    ipcRenderer.invoke(IPC.CHAT_SEND, sessionId, text, options),
  cancelChat: (sessionId: string) => ipcRenderer.invoke(IPC.CHAT_CANCEL, sessionId),

  approveTool: (sessionId: string, callId: string, always?: boolean) => ipcRenderer.invoke(IPC.TOOL_APPROVE, sessionId, callId, always),
  denyTool: (sessionId: string, callId: string) => ipcRenderer.invoke(IPC.TOOL_DENY, sessionId, callId),
  answerQuestion: (sessionId: string, callId: string, answers) => ipcRenderer.invoke(IPC.TOOL_ANSWER_QUESTION, sessionId, callId, answers),

  getPermissions: (sessionId: string) => ipcRenderer.invoke(IPC.PERMISSIONS_GET, sessionId),
  setMode: (sessionId: string, mode: string) => ipcRenderer.invoke(IPC.CHAT_SET_MODE, sessionId, mode),
  getHooks: (sessionId: string) => ipcRenderer.invoke(IPC.HOOKS_GET, sessionId),
  reloadHooks: (sessionId: string) => ipcRenderer.invoke(IPC.HOOKS_RELOAD, sessionId),
  openHooksSettings: (sessionId: string, scope: 'global' | 'project') =>
    ipcRenderer.invoke(IPC.HOOKS_OPEN, sessionId, scope),

  getSandboxGrants: () => ipcRenderer.invoke(IPC.SANDBOX_GRANTS_GET),
  setSandboxGrants: (paths: string[]) => ipcRenderer.invoke(IPC.SANDBOX_GRANTS_SET, paths),
  getForbidRead: () => ipcRenderer.invoke(IPC.SANDBOX_FORBID_READ_GET),
  setForbidRead: (paths: string[]) => ipcRenderer.invoke(IPC.SANDBOX_FORBID_READ_SET, paths),
  getBashSandbox: () => ipcRenderer.invoke(IPC.SANDBOX_BASH_GET),
  setBashSandbox: (enabled: boolean) => ipcRenderer.invoke(IPC.SANDBOX_BASH_SET, enabled),
  getSsrConfig: () => ipcRenderer.invoke(IPC.SSRF_CONFIG_GET),
  setSsrConfig: (cfg: SsrConfigPayload) => ipcRenderer.invoke(IPC.SSRF_CONFIG_SET, cfg),

  listCheckpoints: (sessionId: string) => ipcRenderer.invoke(IPC.CHECKPOINT_LIST, sessionId),
  diffCheckpoint: (sessionId: string, checkpointId: string) => ipcRenderer.invoke(IPC.CHECKPOINT_DIFF, sessionId, checkpointId),
  restoreCheckpoint: (sessionId: string, checkpointId: string) => ipcRenderer.invoke(IPC.CHECKPOINT_RESTORE, sessionId, checkpointId),

  writeClipboard: (text: string) => ipcRenderer.invoke(IPC.CLIPBOARD_WRITE, text),

  listSkills: (cwd: string) => ipcRenderer.invoke(IPC.SKILL_LIST, cwd),
  readSkill: (path: string) => ipcRenderer.invoke(IPC.SKILL_READ, path),
  setSkillDisabled: (cwd: string, slug: string, disabled: boolean) =>
    ipcRenderer.invoke(IPC.SKILL_SET_DISABLED, cwd, slug, disabled),

  readFile: (sessionId: string, path: string) => ipcRenderer.invoke(IPC.FILE_READ, sessionId, path),

  getGitInfo: (cwd: string) => ipcRenderer.invoke(IPC.GIT_INFO, cwd),
  listGitBranches: (cwd: string) => ipcRenderer.invoke(IPC.GIT_BRANCHES, cwd),
  checkoutBranch: (cwd: string, branch: string, strategy) =>
    ipcRenderer.invoke(IPC.GIT_CHECKOUT, cwd, branch, strategy),

  listMcp: (sessionId: string) => ipcRenderer.invoke(IPC.MCP_LIST, sessionId),
  setMcpDisabled: (sessionId: string, name: string, disabled: boolean) =>
    ipcRenderer.invoke(IPC.MCP_SET_DISABLED, sessionId, name, disabled),

  getVersion: () => ipcRenderer.invoke(IPC.APP_VERSION),
  getDefaultCwd: () => ipcRenderer.invoke(IPC.GET_DEFAULT_CWD),
  getModelsConfig: () => ipcRenderer.invoke(IPC.GET_MODELS_CONFIG),
  saveModelsConfig: (cfg) => ipcRenderer.invoke(IPC.SAVE_MODELS_CONFIG, cfg),
  setDefaultModel: (modelId: string) => ipcRenderer.invoke(IPC.SET_DEFAULT_MODEL, modelId),
  selectDirectory: () => ipcRenderer.invoke(IPC.SELECT_DIRECTORY),
  openExternal: (path: string) => ipcRenderer.invoke(IPC.OPEN_EXTERNAL, path),

  renameSession: (id: string, title: string) => ipcRenderer.invoke(IPC.SESSION_RENAME, id, title),
  pinSession: (id: string, pinned: boolean) => ipcRenderer.invoke(IPC.SESSION_PIN, id, pinned),

  createGroup: (name: string) => ipcRenderer.invoke(IPC.GROUP_CREATE, name),
  renameGroup: (id: string, name: string) => ipcRenderer.invoke(IPC.GROUP_RENAME, id, name),
  deleteGroup: (id: string) => ipcRenderer.invoke(IPC.GROUP_DELETE, id),
  moveSession: (sessionId: string, groupId: string | null) => ipcRenderer.invoke(IPC.GROUP_MOVE_SESSION, sessionId, groupId),
  moveSessionToProject: (sessionId: string) => ipcRenderer.invoke(IPC.GROUP_MOVE_SESSION_TO_PROJECT, sessionId),
  listGroups: () => ipcRenderer.invoke(IPC.GROUP_LIST),

  getBridgeStatuses: (sessionIds: string[]) => ipcRenderer.invoke(IPC.BRIDGE_STATUS, sessionIds),

  // Custom (frameless) title bar window controls.
  minimizeWindow: () => ipcRenderer.invoke(IPC.WINDOW_MINIMIZE),
  toggleMaximizeWindow: () => ipcRenderer.invoke(IPC.WINDOW_MAXIMIZE_TOGGLE),
  closeWindow: () => ipcRenderer.invoke(IPC.WINDOW_CLOSE),
  isWindowMaximized: () => ipcRenderer.invoke(IPC.WINDOW_IS_MAXIMIZED),
  onWindowMaximizedChanged: (callback: (maximized: boolean) => void) => {
    const handler = (_: unknown, maximized: boolean) => callback(maximized)
    ipcRenderer.on(IPC.WINDOW_MAXIMIZED_CHANGED, handler)
    return () => { ipcRenderer.removeListener(IPC.WINDOW_MAXIMIZED_CHANGED, handler) }
  },

  onBridgeStatusChanged: (callback: (payload: { sessionIds: string[]; running?: boolean }) => void) => {
    const handler = (_: unknown, data: { sessionIds: string[]; running?: boolean }) => callback(data)
    ipcRenderer.on(IPC.BRIDGE_STATUS_CHANGED, handler)
    return () => { ipcRenderer.removeListener(IPC.BRIDGE_STATUS_CHANGED, handler) }
  },

  onStreamEvent: (callback: (event: SessionStreamEvent) => void) => {
    const handler = (_: unknown, data: SessionStreamEvent) => callback(data)
    ipcRenderer.on(IPC.STREAM_EVENT, handler)
    return () => { ipcRenderer.removeListener(IPC.STREAM_EVENT, handler) }
  },
}

contextBridge.exposeInMainWorld('electronAPI', api)
