import React, { useEffect } from 'react'
import { useStore } from '../stores'
import type { RiskLevel } from '../../shared/types'
import { toolDisplayName, relativePath, editsFromToolInput, uniquePaths } from './MultiEditDiff'
import { useT } from '../useI18n'

const RISK_STYLE: Record<RiskLevel, { border: string; badge: string; labelKey: string }> = {
  safe: { border: 'border-surface-border', badge: 'bg-emerald-500/15 text-emerald-700', labelKey: 'permission.safe' },
  write: { border: 'border-accent/50', badge: 'bg-accent/20 text-accent', labelKey: 'permission.modifies' },
  dangerous: { border: 'border-red-500/50', badge: 'bg-red-500/15 text-red-700', labelKey: 'permission.dangerous' },
}

// Human-readable body for a pending tool call, replacing a raw JSON dump. Known
// tools get a purpose-built line (delete → "Delete file: <path>", bash → the
// command); anything else falls back to formatted JSON so nothing is hidden.
function PermissionBody({ tool, params, cwd }: { tool: string; params: Record<string, unknown>; cwd: string }) {
  const t = useT()
  const str = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v))
  const path = (v: unknown) => (typeof v === 'string' && v ? relativePath(v, cwd) : str(v))
  const p = params || {}

  if (tool === 'delete_file') {
    return (
      <div className="px-3 py-2.5 flex items-start gap-2">
        <div className="min-w-0">
          <div className="text-xs text-ink-soft">{t('permission.deleteFile')}</div>
          <div className="text-xs font-mono text-ink break-all">{path(p.path ?? p.file_path)}</div>
        </div>
      </div>
    )
  }

  if (tool === 'bash') {
    return (
      <div className="px-3 py-2.5">
        <div className="text-[11px] text-ink-faint mb-1">{t('permission.runCommand')}</div>
        <pre className="text-xs font-mono text-ink bg-surface/60 rounded px-2 py-1.5 overflow-x-auto max-h-32 whitespace-pre-wrap break-words">
          {str(p.command)}
        </pre>
      </div>
    )
  }

  // File write/edit tools (write_file / multi_write / search_replace /
  // multi_edit): show a friendly file list instead of a raw JSON dump, so the
  // user sees at a glance which paths are being touched.
  const edits = editsFromToolInput(tool, p)
  if (edits !== null && edits.length > 0) {
    const isWrite = tool === 'write_file' || tool === 'multi_write'
    const count = edits.length
    const verbLine = isWrite
      ? (count === 1 ? t('permission.willWriteOne') : t('permission.willWrite', { count }))
      : (count === 1 ? t('permission.willEditOne') : t('permission.willEdit', { count }))
    const paths = uniquePaths(edits)
    return (
      <div className="px-3 py-2.5 space-y-1">
        <div className="text-[11px] text-ink-faint">{verbLine}</div>
        {paths.map((pth) => (
          <div key={pth} className="text-xs font-mono text-ink break-all">
            {relativePath(pth, cwd)}
          </div>
        ))}
      </div>
    )
  }

  // Generic fallback: still readable, but complete.
  return (
    <pre className="text-xs font-mono text-ink-faint px-3 py-2 overflow-x-auto max-h-32 whitespace-pre-wrap break-words">
      {JSON.stringify(p, null, 2)}
    </pre>
  )
}

// A sandbox-escalation request: the tool asked for danger-full-access, so the
// user's reason (`justification`) is the whole point of the prompt — show it
// prominently instead of burying it in a JSON dump.
function EscalationNotice({ params }: { params: Record<string, unknown> }) {
  const t = useT()
  const isEsc = params?.sandbox_permissions === 'danger-full-access'
  const just = typeof params?.justification === 'string' ? params.justification : ''
  if (!isEsc) return null
  return (
    <div className="mx-3 mt-2 mb-2 px-2.5 py-2 rounded border border-orange-500/30 bg-orange-500/10">
      <div className="text-[11px] font-semibold text-orange-600">
        {t('permission.escalationTitle')}
      </div>
      {just && <div className="text-xs text-ink-soft mt-0.5">{t('permission.reason', { reason: just })}</div>}
    </div>
  )
}

export default function PermissionCard() {
  const t = useT()
  const pending = useStore((s) => s.pendingPermission)
  const cwd = useStore((s) => s.workingDir)
  const approve = useStore((s) => s.approveTool)
  const deny = useStore((s) => s.denyTool)

  // Keyboard shortcuts: y = approve, a = always, n/esc = deny (Claude-style).
  useEffect(() => {
    if (!pending) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'y') { e.preventDefault(); approve(pending.call_id, false) }
      else if (e.key === 'a') { e.preventDefault(); approve(pending.call_id, true) }
      else if (e.key === 'n' || e.key === 'Escape') { e.preventDefault(); deny(pending.call_id) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pending, approve, deny])

  if (!pending) return null
  const style = RISK_STYLE[pending.risk_level]
  const isEscalation = (pending.params as Record<string, unknown> | undefined)?.sandbox_permissions === 'danger-full-access'

  return (
    <div className={`mx-4 mb-4 border rounded-lg bg-surface-raised ${style.border}`}>
      <div className="flex items-center gap-2 px-3 py-2 border-b border-surface-border">
        <span className="text-sm font-semibold text-ink">{t('permission.allow')}</span>
        <span className="text-xs font-mono text-ink-soft">{toolDisplayName(pending.tool_name)}</span>
        {isEscalation && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-orange-500/20 text-orange-600 font-semibold">{t('permission.escalationBadge')}</span>
        )}
        <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded ${style.badge}`}>{t(style.labelKey)}</span>
      </div>

      <EscalationNotice params={pending.params as Record<string, unknown>} />

      <PermissionBody tool={pending.tool_name} params={pending.params} cwd={cwd} />

      <div className="flex gap-2 px-3 py-2 border-t border-surface-border">
        <button
          onClick={() => approve(pending.call_id, false)}
          className="px-3 py-1.5 bg-accent hover:bg-accent-hover text-accent-ink text-xs rounded font-medium transition-colors"
        >{t('permission.approve')}</button>
        <button
          onClick={() => approve(pending.call_id, true)}
          className="px-3 py-1.5 bg-surface-border hover:bg-ink-faint/40 text-ink text-xs rounded font-medium transition-colors"
        >{t('permission.alwaysApprove')}</button>
        <button
          onClick={() => deny(pending.call_id)}
          className="px-3 py-1.5 bg-transparent hover:bg-red-500/10 text-ink-soft hover:text-red-600 text-xs rounded font-medium transition-colors ml-auto"
        >{t('permission.deny')}</button>
      </div>
    </div>
  )
}
