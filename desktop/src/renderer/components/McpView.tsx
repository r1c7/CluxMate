import React, { useEffect, useState } from 'react'
import { useStore } from '../stores'
import type { McpServer } from '../../shared/types'
import { useT } from '../useI18n'
import { formatTime } from '../../shared/format-time'

// Transport badge — local (stdio) is muted gray (data stays on this machine),
// remote (http) is orange (every tool call is a network egress, data may leave).
// Fallback for unknown transports so the UI doesn't crash if Python adds a new
// transport type before this map is updated.
const TRANSPORT_BADGE: Record<string, { labelKey: string; cls: string }> = {
  local: { labelKey: 'mcp.local', cls: 'bg-slate-500/15 text-slate-600 border-slate-500/30' },
  remote: { labelKey: 'mcp.remote', cls: 'bg-orange-500/15 text-orange-600 border-orange-500/30' },
}
const DEFAULT_TRANSPORT_BADGE: { labelKey: string; cls: string } = {
  labelKey: '',
  cls: 'bg-slate-500/15 text-slate-600 border-slate-500/30',
}
function transportBadge(t: string): { labelKey: string; cls: string } {
  return TRANSPORT_BADGE[t] ?? { ...DEFAULT_TRANSPORT_BADGE, labelKey: t }
}

const STATUS_DOT: Record<string, string> = {
  connected: 'bg-emerald-500',
  needs_auth: 'bg-amber-500',
  failed: 'bg-red-500',
  disabled: 'bg-slate-400',
  disconnected: 'bg-slate-300',
}

// Copyable config samples (language-neutral — they are JSON, and the field
// names must match cluxmate/core/mcp.py byte for byte, so they are NOT i18n).
// Deliberately free of `${` sequences: inside a TS template literal those
// would be interpolation, and a sample that silently renders as an empty
// string is worse than one that teaches the feature in the field notes.
const STDIO_SAMPLE = `{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path/to/dir"],
      "env": { "SOME_TOKEN": "…" },
      "disabled": false
    }
  }
}`

const HTTP_SAMPLE = `{
  "mcpServers": {
    "linear": {
      "url": "https://mcp.linear.app/mcp",
      "oauth": {
        "client_id": "…",
        "client_secret_env": "LINEAR_MCP_SECRET",
        "scopes": "read write",
        "callback_port": 0
      }
    }
  }
}`

// Main-area view (swaps in for ChatView) that lists configured MCP servers on
// the left and the selected server's tools on the right. The only mutations are
// the mcp.json disable toggle (takes effect next session, not hot-swapped), the
// OAuth login/logout buttons (hot-swapped by the Python side) and the selected
// server.
// Mirrors SkillsView's two-pane layout.
export default function McpView() {
  const t = useT()
  const servers = useStore((s) => s.mcpServers)
  const loading = useStore((s) => s.mcpLoading)
  const selectedName = useStore((s) => s.selectedMcpServer)
  const showMcp = useStore((s) => s.showMcp)
  const selectMcpServer = useStore((s) => s.selectMcpServer)
  const setMcpDisabled = useStore((s) => s.setMcpDisabled)
  const authPending = useStore((s) => s.authPending)
  const startMcpAuth = useStore((s) => s.startMcpAuth)
  const logoutMcp = useStore((s) => s.logoutMcp)
  const activeSessionId = useStore((s) => s.activeSessionId)

  // Names toggled this view — their new disabled state is written to mcp.json
  // but the running bridge won't hot-swap, so we flag them "pending restart".
  const [pendingRestart, setPendingRestart] = useState<Set<string>>(new Set())

  // 'list' | 'help' — the help reference takes the whole area (same shape as
  // HooksView's help page) instead of overlaying the list.
  const [view, setView] = useState<'list' | 'help'>('list')

  // Re-fetch when the active session changes — each session has its own
  // Python process owning MCP subprocesses. Clear pending flags too: a new
  // session's bridge re-reads mcp.json, so the toggle is no longer pending.
  useEffect(() => {
    showMcp()
    setPendingRestart(new Set())
  }, [activeSessionId])

  const onToggle = (name: string, disabled: boolean) => {
    setMcpDisabled(name, disabled)
    setPendingRestart((prev) => new Set(prev).add(name))
  }

  const selected = servers.find((s) => s.name === selectedName)

  if (view === 'help') {
    return <HelpPage onBack={() => setView('list')} />
  }

  return (
    <div className="flex-1 flex min-h-0">
      {/* Server list */}
      <div className="w-64 flex-shrink-0 border-r border-surface-border flex flex-col">
        <div className="px-3 h-9 flex items-center gap-2 border-b border-surface-border flex-shrink-0">
          <span className="text-xs font-semibold text-ink-soft">{t('mcp.servers')}</span>
          <span className="text-[10px] text-ink-faint">{servers.length}</span>
          {/* Help lives in the left header, not the right pane: the right pane
              is empty until a server is selected, so an entry point there would
              be invisible in the state that needs it most. */}
          <button
            onClick={() => setView('help')}
            className="ml-auto text-xs px-2.5 py-1 rounded-md border border-surface-border text-ink-soft hover:text-ink hover:bg-sidebar-hover transition-colors flex-shrink-0"
          >
            {t('mcp.help')}
          </button>
        </div>
        <div className="flex-1 overflow-y-auto py-1">
          {loading && servers.length === 0 ? (
            <p className="text-xs text-ink-faint px-3 py-3">{t('mcp.loading')}</p>
          ) : servers.length === 0 ? (
            <div className="text-xs text-ink-faint px-3 py-3 leading-relaxed">
              {t('mcp.none')}
              <pre className="mt-2 text-[10px] font-mono text-ink-faint/80 bg-surface-raised/30 rounded p-2 overflow-x-auto">
{`{
  "mcpServers": {
    "name": {
      "command": "...",
      "args": [...]
    }
  }
}`}
              </pre>
            </div>
          ) : (
            servers.map((s) => {
              const badge = transportBadge(s.transport)
              const isSel = s.name === selectedName
              const dot = STATUS_DOT[s.status] ?? STATUS_DOT.disconnected
              return (
                <div
                  key={s.name}
                  onClick={() => selectMcpServer(s.name)}
                  className={`w-full text-left px-3 py-2 border-l-2 transition-colors cursor-pointer ${
                    isSel
                      ? 'bg-sidebar-active border-accent'
                      : 'border-transparent hover:bg-sidebar-hover'
                  }`}
                >
                  <div className="flex items-center gap-1.5">
                    <span className={`inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${dot}`} />
                    <span className="text-sm text-ink truncate flex-1 min-w-0">{s.name}</span>
                    <span className={`text-[9px] px-1 py-0.5 rounded border flex-shrink-0 ${badge.cls}`}>
                      {badge.labelKey ? t(badge.labelKey) : badge.labelKey}
                    </span>
                    {/* Disable toggle — writes to project mcp.json, takes
                        effect next session (no hot-swap). Optimistic. */}
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        onToggle(s.name, !s.disabled)
                      }}
                      title={s.disabled ? t('skills.toggleDisabledTitle') : t('skills.toggleEnabledTitle')}
                      className={`relative w-9 h-5 rounded-full transition-colors flex-shrink-0 hover:opacity-80 ${
                        s.disabled ? 'bg-surface-border' : 'bg-accent'
                      }`}
                    >
                      <span
                        className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${
                          s.disabled ? 'left-0.5' : 'left-[18px]'
                        }`}
                      />
                    </button>
                  </div>
                  <div className="text-xs text-ink-faint truncate mt-0.5">
                    {t('mcp.tools', { count: s.tools.length, plural: s.tools.length === 1 ? '' : 's' })}
                    {/* needs_auth is actionable (the 401 explanation lives in the
                        amber hint + the login button), not a failure — so the red
                        error span stays reserved for genuine breakage. */}
                    {s.error && s.status !== 'needs_auth' && (
                      <span className="text-red-600 ml-2 truncate">{s.error}</span>
                    )}
                    {s.status === 'needs_auth' && (
                      <span className="text-amber-600 ml-2">{t('mcp.needsAuth')}</span>
                    )}
                    {pendingRestart.has(s.name) && (
                      <span className="text-amber-600 ml-2 truncate">{t('mcp.restartNote')}</span>
                    )}
                  </div>
                </div>
              )
            })
          )}
        </div>
      </div>

      {/* Tools preview for the selected server. */}
      <div className="flex-1 flex flex-col min-w-0">
        {selected ? (
          <>
            <div className="px-4 h-9 flex items-center gap-2 border-b border-surface-border flex-shrink-0">
              <span className="text-xs font-mono text-ink-soft truncate">{selected.name}</span>
              {(() => {
                const selBadge = transportBadge(selected.transport)
                return (
                  <span className={`text-[9px] px-1 py-0.5 rounded border ${selBadge.cls}`}>
                    {selBadge.labelKey ? t(selBadge.labelKey) : selBadge.labelKey}
                  </span>
                )
              })()}
              {selected.oauth?.enabled && (
                selected.oauth.authenticated ? (
                  <button
                    onClick={() => logoutMcp(selected.name)}
                    className="text-[10px] px-2 py-0.5 rounded border border-surface-border text-ink-soft hover:bg-surface-raised"
                  >
                    {t('mcp.logout')}
                  </button>
                ) : (
                  <button
                    onClick={() => startMcpAuth(selected.name)}
                    disabled={authPending === selected.name}
                    className="text-[10px] px-2 py-0.5 rounded border border-accent text-accent hover:bg-accent/10 disabled:opacity-50"
                  >
                    {authPending === selected.name ? t('mcp.authPending') : t('mcp.login')}
                  </button>
                )
              )}
              {selected.oauth?.authenticated && selected.oauth.expires_at && (
                <span className="text-[10px] text-ink-faint">
                  {t('mcp.expiresIn', { when: formatTime(selected.oauth.expires_at * 1000) })}
                </span>
              )}
              <span className="text-[10px] text-ink-faint truncate ml-auto">
                {t('mcp.statusLine', { count: selected.tools.length, status: selected.status })}
              </span>
            </div>
            {/* The server carries BOTH an OAuth config and a static
                Authorization header: the Python loader lets OAuth win, so say so
                rather than leaving the user guessing which credential is used. */}
            {selected.oauth?.conflict === 'static_header' && (
              <p className="text-[10px] text-amber-600 px-6 pt-2">{t('mcp.oauthConflict')}</p>
            )}
            <div className="flex-1 overflow-y-auto px-6 py-4">
              {selected.tools.length === 0 ? (
                <p className="text-xs text-ink-faint italic">
                  {/* A 401 needs_auth server legitimately returns no tools; the
                      generic "server may have failed to load" wording would frame
                      a normal login prompt as breakage. */}
                  {selected.status === 'needs_auth' ? t('mcp.needsAuthHint') : t('mcp.noTools')}
                </p>
              ) : (
                <div className="space-y-4">
                  {selected.tools.map((tool) => (
                    <ToolEntry key={tool.name} tool={tool} serverName={selected.name} />
                  ))}
                </div>
              )}
            </div>
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center text-ink-faint text-sm">
            {t('mcp.selectHint')}
          </div>
        )}
      </div>
    </div>
  )
}

// Full-page config reference, rendered in place of the server list. Static
// content only — it never reads or writes mcp.json.
//
// `min-h-0` is load-bearing: this root is a flex item of the app's column, and
// a flex item's automatic minimum size is its CONTENT height, so without it the
// page grows past the viewport (clipped by the ancestor `overflow-hidden`) and
// the scrolling body below never gets a bounded height to scroll within. Same
// reason SubagentFocusView's root carries it.
function HelpPage({ onBack }: { onBack: () => void }) {
  const t = useT()
  return (
    <div className="flex-1 flex flex-col min-h-0 min-w-0">
      <div className="h-9 border-b border-surface-border flex items-center gap-2 px-4 flex-shrink-0">
        <button
          onClick={onBack}
          className="text-xs text-accent hover:text-accent-hover transition-colors flex items-center gap-1"
        >
          <span aria-hidden>←</span>
          {t('mcp.helpBack')}
        </button>
        <span className="text-xs font-semibold text-ink-soft">{t('mcp.help')}</span>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4 max-w-2xl select-text">
        <p className="text-xs text-ink-soft leading-relaxed">{t('mcp.helpIntro')}</p>

        <div className="mt-4">
          <h4 className="text-[10px] uppercase tracking-wide text-ink-faint/70 mb-1">
            {t('mcp.helpLocations')}
          </h4>
          <p className="text-[11px] font-mono text-ink-soft/90 whitespace-pre-line">
            {t('mcp.helpLocationsBody')}
          </p>
        </div>

        <SampleBlock title={t('mcp.helpStdio')} code={STDIO_SAMPLE} />
        <SampleBlock title={t('mcp.helpHttp')} code={HTTP_SAMPLE} />
        <p className="mt-2 text-[11px] text-ink-faint leading-relaxed">{t('mcp.helpHttpNote')}</p>

        <div className="mt-4">
          <h4 className="text-[10px] uppercase tracking-wide text-ink-faint/70 mb-1">
            {t('mcp.helpFields')}
          </h4>
          <ul className="text-[11px] text-ink-soft list-disc list-inside space-y-1 leading-relaxed">
            <li>{t('mcp.helpFieldCommand')}</li>
            <li>{t('mcp.helpFieldDisabled')}</li>
            <li>{t('mcp.helpFieldEnvVar')}</li>
            <li>{t('mcp.helpFieldAuthEnv')}</li>
            <li>{t('mcp.helpFieldNaming')}</li>
          </ul>
        </div>

        <p className="mt-4 text-[11px] text-ink-soft leading-relaxed">{t('mcp.helpSandbox')}</p>

        <div className="mt-4">
          <h4 className="text-[10px] uppercase tracking-wide text-ink-faint/70 mb-1">
            {t('mcp.helpNotes')}
          </h4>
          <ul className="text-[11px] text-ink-faint list-disc list-inside space-y-1">
            <li>{t('mcp.helpNoteRestart')}</li>
            <li>{t('mcp.helpNoteToggle')}</li>
            <li>{t('mcp.helpNoteFailed')}</li>
          </ul>
        </div>
      </div>
    </div>
  )
}

// One copyable JSON block: title row + copy button + monospace body.
function SampleBlock({ title, code }: { title: string; code: string }) {
  const t = useT()
  return (
    <div className="mt-4 rounded-md border border-surface-border bg-surface-raised/30 overflow-hidden">
      <div className="px-3 py-1.5 border-b border-surface-border flex items-center gap-2">
        <span className="text-[10px] uppercase tracking-wide text-ink-faint/70">{title}</span>
        <CopyButton text={code} label={t('common.copy')} copiedLabel={t('mcp.helpCopied')} />
      </div>
      <pre className="px-3 py-2 text-[11px] font-mono text-ink-soft/90 overflow-x-auto">{code}</pre>
    </div>
  )
}

// Copy through the main process: the renderer's navigator.clipboard is flaky
// under file:// (see the CLIPBOARD_WRITE handler in ipc-handlers.ts).
function CopyButton({ text, label, copiedLabel }: { text: string; label: string; copiedLabel: string }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await window.electronAPI.writeClipboard(text)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }
  return (
    <button
      onClick={copy}
      className="ml-auto text-[11px] text-accent hover:text-accent-hover transition-colors"
    >
      {copied ? copiedLabel : label}
    </button>
  )
}

function ToolEntry({
  tool,
  serverName,
}: {
  tool: { name: string; description: string; input_schema: Record<string, unknown> }
  serverName: string
}) {
  const t = useT()
  return (
    <div className="rounded-md border border-surface-border bg-surface-raised/30 overflow-hidden">
      <div className="px-3 py-2 border-b border-surface-border flex items-center gap-2">
        <span className="text-xs font-mono text-accent truncate">
          mcp__{serverName}__{tool.name}
        </span>
      </div>
      <div className="px-3 py-2 space-y-2">
        {tool.description && (
          <p className="text-xs text-ink-soft leading-relaxed">{tool.description}</p>
        )}
        <details className="text-xs">
          <summary className="text-ink-faint cursor-pointer hover:text-ink-soft">
            {t('mcp.inputSchema')}
          </summary>
          <pre className="mt-1 text-[10px] font-mono text-ink-faint/80 bg-surface-raised/40 rounded p-2 overflow-x-auto">
            {JSON.stringify(tool.input_schema, null, 2)}
          </pre>
        </details>
      </div>
    </div>
  )
}
