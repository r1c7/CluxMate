import React, { useEffect } from 'react'
import { useStore } from '../stores'
import type { RiskLevel } from '../../shared/types'
import { MultiEditFileList, editStats, toolDisplayName } from './MultiEditDiff'
import { useT } from '../useI18n'

// Aligned with PermissionCard so the two approval surfaces read as one system.
const RISK_STYLE: Record<RiskLevel, { border: string; badge: string; labelKey: string }> = {
  safe: { border: 'border-surface-border', badge: 'bg-emerald-500/15 text-emerald-700', labelKey: 'permission.safe' },
  write: { border: 'border-accent/50', badge: 'bg-accent/20 text-accent', labelKey: 'permission.modifies' },
  dangerous: { border: 'border-red-500/50', badge: 'bg-red-500/15 text-red-700', labelKey: 'permission.dangerous' },
  critical: { border: 'border-fuchsia-600/60', badge: 'bg-fuchsia-600/15 text-fuchsia-700', labelKey: 'permission.critical' },
}

export default function BatchEditCard() {
  const t = useT()
  const pending = useStore((s) => s.pendingBatchEdit)
  const approveBatchEdit = useStore((s) => s.approveBatchEdit)
  const denyTool = useStore((s) => s.denyTool)

  // Keyboard shortcuts mirror PermissionCard: y = approve, a = always, n/esc = deny.
  useEffect(() => {
    if (!pending) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'y') { e.preventDefault(); approveBatchEdit(pending.call_id) }
      else if (e.key === 'a') { e.preventDefault(); approveBatchEdit(pending.call_id, true) }
      else if (e.key === 'n' || e.key === 'Escape') { e.preventDefault(); denyTool(pending.call_id) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [pending, approveBatchEdit, denyTool])

  if (!pending) return null
  const style = RISK_STYLE[pending.risk_level]
  const stats = editStats(pending.edits)

  return (
    <div className={`mx-4 mb-4 border rounded-lg bg-surface-raised ${style.border}`}>
      {/* Header — tool name · file count · totals · risk badge */}
      <div className="flex items-center gap-2 px-3 py-2 border-b border-surface-border">
        <span className="text-sm font-mono font-semibold text-ink">{toolDisplayName(pending.tool_name)}</span>
        {pending.edits.length > 1 && (
          <span className="text-xs text-ink-faint">{t('toolCard.files', { count: pending.edits.length, plural: pending.edits.length === 1 ? '' : 's' })}</span>
        )}
        <span className="text-[11px] font-mono">
          <span className="text-emerald-600">+{stats.add}</span>
          <span className="text-red-600 ml-1.5">−{stats.del}</span>
        </span>
        <span className={`ml-auto text-[10px] px-1.5 py-0.5 rounded ${style.badge}`}>{t(style.labelKey)}</span>
      </div>

      {/* File list — first file expanded for review */}
      <div className="max-h-96 overflow-y-auto">
        <MultiEditFileList edits={pending.edits} defaultOpenFirst />
      </div>

      {/* Footer — whole-turn approval, Claude-Code style */}
      <div className="flex gap-2 px-3 py-2 border-t border-surface-border">
        <button
          onClick={() => approveBatchEdit(pending.call_id)}
          className="px-3 py-1.5 bg-accent hover:bg-accent-hover text-accent-ink text-xs rounded font-medium transition-colors"
        >{t('permission.approve')}</button>
        <button
          onClick={() => approveBatchEdit(pending.call_id, true)}
          className="px-3 py-1.5 bg-surface-border hover:bg-ink-faint/40 text-ink text-xs rounded font-medium transition-colors"
        >{t('permission.alwaysApprove')}</button>
        <button
          onClick={() => denyTool(pending.call_id)}
          className="px-3 py-1.5 bg-transparent hover:bg-red-500/10 text-ink-soft hover:text-red-600 text-xs rounded font-medium transition-colors ml-auto"
        >{t('permission.deny')}</button>
      </div>
    </div>
  )
}
