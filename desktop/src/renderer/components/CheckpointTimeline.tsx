import React, { useEffect, useState } from 'react'
import { useStore } from '../stores'
import type { Checkpoint } from '../../shared/types'
import { useT } from '../useI18n'
import { tGlobal } from '../i18n'

function relativeTime(iso: string): string {
  const t = Date.parse(iso)
  const ms = Number.isNaN(t) ? Number(iso) * 1000 : t
  if (Number.isNaN(ms)) return ''
  const diff = Date.now() - ms
  const s = Math.floor(diff / 1000)
  if (s < 60) return tGlobal('checkpoint.time.s', { count: s })
  const m = Math.floor(s / 60)
  if (m < 60) return tGlobal('checkpoint.time.m', { count: m })
  const h = Math.floor(m / 60)
  if (h < 24) return tGlobal('checkpoint.time.h', { count: h })
  return tGlobal('checkpoint.time.d', { count: Math.floor(h / 24) })
}

// Right-side slide-out panel listing the session's workspace checkpoints,
// newest first. Each entry can open a diff (vs its parent checkpoint) or
// restore the workspace to that point. Restore only reverts files the agent
// changed across snapshots — the user's manual edits are preserved.
export default function CheckpointTimeline() {
  const t = useT()
  const open = useStore((s) => s.checkpointsOpen)
  const checkpoints = useStore((s) => s.checkpoints)
  const loadCheckpoints = useStore((s) => s.loadCheckpoints)
  const restoreCheckpoint = useStore((s) => s.restoreCheckpoint)
  const toggle = useStore((s) => s.toggleCheckpoints)
  const activeSessionId = useStore((s) => s.activeSessionId)
  const openDiff = useStore((s) => s.openDiff)

  const [busy, setBusy] = useState<string | null>(null)

  useEffect(() => {
    if (open) loadCheckpoints()
  }, [open, activeSessionId])

  if (!open) return null

  const viewDiff = async (cp: Checkpoint) => {
    setBusy(cp.id)
    try {
      await openDiff(cp.id, cp.label || cp.id.slice(0, 7))
    } finally {
      setBusy(null)
    }
  }

  const doRestore = async (cp: Checkpoint) => {
    const ok = window.confirm(
      t('checkpoint.restoreConfirm', { label: cp.label || cp.id.slice(0, 7) })
    )
    if (!ok) return
    setBusy(cp.id)
    try {
      await restoreCheckpoint(cp.id)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="h-full flex flex-col bg-surface">
      <div className="flex items-center gap-2 px-3 h-9 border-b border-surface-border flex-shrink-0">
        <span className="text-xs font-semibold text-ink-soft">{t('checkpoint.title')}</span>
        <button
          onClick={() => toggle(false)}
          className="text-ink-faint hover:text-ink text-base px-1 ml-auto transition-colors"
          title={t('checkpoint.close')}
        >×</button>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-2 space-y-1.5">
        {checkpoints.length === 0 ? (
          <p className="text-xs text-ink-faint px-1 py-2">
            {t('checkpoint.none')}
          </p>
        ) : (
          checkpoints.map((cp) => (
            <div
              key={cp.id}
              className="rounded-md border border-surface-border bg-surface-raised/50 px-2.5 py-2"
            >
              <div className="flex items-center gap-2">
                <span className="inline-block w-1.5 h-1.5 rounded-full bg-accent flex-shrink-0" />
                <span className="text-xs text-ink truncate flex-1 min-w-0" title={cp.label}>
                  {cp.label || t('checkpoint.empty')}
                </span>
              </div>
              <div className="flex items-center gap-2 mt-1 pl-3.5">
                <span className="text-[10px] text-ink-faint">{relativeTime(cp.timestamp)}</span>
                <span className="text-[10px] text-ink-faint">· {t('checkpoint.files', { count: cp.files_changed, plural: cp.files_changed === 1 ? '' : 's' })}</span>
                <span className="text-[10px] font-mono text-ink-faint/70">{cp.id.slice(0, 7)}</span>
              </div>
              <div className="flex items-center gap-1.5 mt-1.5 pl-3.5">
                <button
                  onClick={() => viewDiff(cp)}
                  disabled={busy === cp.id}
                  className="text-[11px] px-2 py-0.5 rounded border border-surface-border text-ink-soft hover:bg-surface-raised hover:text-ink disabled:opacity-50 transition-colors"
                >{t('checkpoint.viewDiff')}</button>
                <button
                  onClick={() => doRestore(cp)}
                  disabled={busy === cp.id}
                  className="text-[11px] px-2 py-0.5 rounded border border-accent/40 text-accent hover:bg-accent/15 disabled:opacity-50 transition-colors"
                >{t('checkpoint.restore')}</button>
              </div>
            </div>
          ))
        )}
      </div>
    </div>
  )
}
