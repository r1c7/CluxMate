import React, { useEffect, useState } from 'react'
import { useStore } from '../stores'
import type { McpServer } from '../../shared/types'
import { useT } from '../useI18n'

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
  failed: 'bg-red-500',
  disabled: 'bg-slate-400',
  disconnected: 'bg-slate-300',
}

// Main-area view (swaps in for ChatView) that lists configured MCP servers on
// the left and the selected server's tools on the right. Read-only — toggle
// is the only mutation, and it writes to mcp.json (takes effect next session,
// not hot-swapped). Mirrors SkillsView's two-pane layout.
export default function McpView() {
  const t = useT()
  const servers = useStore((s) => s.mcpServers)
  const loading = useStore((s) => s.mcpLoading)
  const selectedName = useStore((s) => s.selectedMcpServer)
  const showMcp = useStore((s) => s.showMcp)
  const selectMcpServer = useStore((s) => s.selectMcpServer)
  const setMcpDisabled = useStore((s) => s.setMcpDisabled)
  const activeSessionId = useStore((s) => s.activeSessionId)

  // Names toggled this view — their new disabled state is written to mcp.json
  // but the running bridge won't hot-swap, so we flag them "pending restart".
  const [pendingRestart, setPendingRestart] = useState<Set<string>>(new Set())

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

  return (
    <div className="flex-1 flex min-h-0">
      {/* Server list */}
      <div className="w-64 flex-shrink-0 border-r border-surface-border flex flex-col">
        <div className="px-3 h-9 flex items-center border-b border-surface-border flex-shrink-0">
          <span className="text-xs font-semibold text-ink-soft">{t('mcp.servers')}</span>
          <span className="text-[10px] text-ink-faint ml-auto">{servers.length}</span>
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
                    {s.error && <span className="text-red-600 ml-2 truncate">{s.error}</span>}
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
              <span className="text-[10px] text-ink-faint truncate ml-auto">
                {t('mcp.statusLine', { count: selected.tools.length, status: selected.status })}
              </span>
            </div>
            <div className="flex-1 overflow-y-auto px-6 py-4">
              {selected.tools.length === 0 ? (
                <p className="text-xs text-ink-faint italic">
                  {t('mcp.noTools')}
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
