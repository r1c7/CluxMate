export const IPC = {
  SESSION_LIST:   'session:list',
  SESSION_SEARCH: 'session:search',
  SESSION_CREATE: 'session:create',
  SESSION_DELETE: 'session:delete',
  SESSION_SWITCH: 'session:switch',
  SESSION_UPDATE_CWD: 'session:update-cwd',
  SESSION_SET_MODEL: 'session:set-model',
  SESSION_SAVE_DISPLAY: 'session:save-display',
  SESSION_TRUNCATE: 'session:truncate',
  SESSION_REPLAY: 'session:replay',
  SESSION_CONTEXT: 'session:context',

  CHAT_SEND:      'chat:send',
  CHAT_CANCEL:    'chat:cancel',
  CHAT_SET_MODE:  'chat:set-mode',

  TOOL_APPROVE:   'tool:approve',
  TOOL_DENY:      'tool:deny',
  TOOL_ANSWER_QUESTION: 'tool:answer-question',

  PERMISSIONS_GET:    'permissions:get',

  HOOKS_GET:          'hooks:get',
  HOOKS_RELOAD:       'hooks:reload',
  HOOKS_OPEN:         'hooks:open',

  SANDBOX_GRANTS_GET: 'sandbox:grants-get',
  SANDBOX_GRANTS_SET: 'sandbox:grants-set',
  SANDBOX_FORBID_READ_GET: 'sandbox:forbid-read-get',
  SANDBOX_FORBID_READ_SET: 'sandbox:forbid-read-set',
  SANDBOX_BASH_GET: 'sandbox:bash-get',
  SANDBOX_BASH_SET: 'sandbox:bash-set',
  SSRF_CONFIG_GET: 'ssrf:config-get',
  SSRF_CONFIG_SET: 'ssrf:config-set',

  CHECKPOINT_LIST:    'checkpoint:list',
  CHECKPOINT_DIFF:    'checkpoint:diff',
  CHECKPOINT_RESTORE: 'checkpoint:restore',

  STREAM_EVENT:   'chat:stream',

  BRIDGE_STATUS_CHANGED: 'session:bridge-status-changed',

  CLIPBOARD_WRITE: 'app:clipboard-write',

  SKILL_LIST:     'skill:list',
  SKILL_READ:     'skill:read',
  SKILL_SET_DISABLED: 'skill:set-disabled',

  FILE_READ:      'file:read',

  GIT_INFO:      'git:info',
  GIT_BRANCHES:  'git:branches',
  GIT_CHECKOUT:  'git:checkout',

  MCP_LIST:        'mcp:list',
  MCP_SET_DISABLED: 'mcp:set-disabled',

  APP_VERSION:    'app:version',
  GET_DEFAULT_CWD: 'app:default-cwd',
  GET_MODELS_CONFIG: 'app:models-config',
  SAVE_MODELS_CONFIG: 'app:models-config-save',
  SET_DEFAULT_MODEL: 'app:default-model',
  SELECT_DIRECTORY: 'app:select-directory',
  OPEN_EXTERNAL:  'app:open-external',

  SESSION_RENAME: 'session:rename',
  SESSION_PIN: 'session:pin',

  BRIDGE_STATUS: 'session:bridge-status',

  GROUP_CREATE: 'group:create',
  GROUP_RENAME: 'group:rename',
  GROUP_DELETE: 'group:delete',
  GROUP_MOVE_SESSION: 'group:move-session',
  GROUP_MOVE_SESSION_TO_PROJECT: 'group:move-session-to-project',
  GROUP_LIST: 'group:list',

  WINDOW_MINIMIZE: 'window:minimize',
  WINDOW_MAXIMIZE_TOGGLE: 'window:maximize-toggle',
  WINDOW_CLOSE: 'window:close',
  WINDOW_IS_MAXIMIZED: 'window:is-maximized',
  WINDOW_MAXIMIZED_CHANGED: 'window:maximized-changed',
} as const
