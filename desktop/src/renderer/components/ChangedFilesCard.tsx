import React, { useState } from 'react'
import type { TurnFileChange } from '../../shared/types'
import { useStore } from '../stores'
import { useT } from '../useI18n'

// Status glyph + color, Claude-Code-desktop style: a single-letter badge in a
// tinted pill (green add / coral modify / red delete). Labels are i18n keys.
const STATUS: Record<TurnFileChange['status'], { glyph: string; cls: string; labelKey: string }> = {
  A: { glyph: '+', cls: 'text-emerald-700 border-emerald-500/30 bg-emerald-500/10', labelKey: 'changedFiles.added' },
  M: { glyph: '~', cls: 'text-accent border-accent/30 bg-accent/10', labelKey: 'changedFiles.modified' },
  D: { glyph: '−', cls: 'text-red-700 border-red-500/30 bg-red-500/10', labelKey: 'changedFiles.deleted' },
}

function basename(p: string): string {
  const parts = p.split('/')
  return parts[parts.length - 1] || p
}
function dirname(p: string): string {
  const i = p.lastIndexOf('/')
  return i > 0 ? p.slice(0, i) : ''
}

// Inline card shown at the end of an agent turn listing every file that turn
// changed. Clicking a file opens the diff overlay (lazy content fetch via the
// checkpoint's diff()). Mirrors Claude Code desktop's post-turn file summary.
// Collapsed by default — click the header to expand.
export default function ChangedFilesCard({
  checkpointId, files, label,
}: {
  checkpointId: string
  files: TurnFileChange[]
  label: string
}) {
  const t = useT()
  const [expanded, setExpanded] = useState(false)
  const openDiff = useStore((s) => s.openDiff)
  const diffView = useStore((s) => s.diffView)
  if (!files || files.length === 0) return null

  // Which file (if any) this card currently has open in the dock — for
  // highlighting the active row so the open/close toggle is obvious.
  const openPath =
    diffView && diffView.checkpointId === checkpointId ? diffView.activePath : undefined

  const totalAdd = files.reduce((n, f) => n + f.additions, 0)
  const totalDel = files.reduce((n, f) => n + f.deletions, 0)

  return (
    <div className="rounded-md border border-surface-border bg-surface-raised/40 overflow-hidden">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full flex items-center gap-2 px-2.5 py-1.5 border-b border-surface-border text-left transition-colors hover:bg-surface-raised"
      >
        <span className={`text-[10px] text-ink-faint transition-transform ${expanded ? 'rotate-90' : ''}`}>▶</span>
        <span className="text-[10px] uppercase tracking-wide text-ink-faint/70">{t('changedFiles.thisTurn')}</span>
        <span className="text-[11px] text-ink-faint">{t('changedFiles.files', { count: files.length, plural: files.length === 1 ? '' : 's' })}</span>
        <span className="ml-auto text-[11px] font-mono">
          <span className="text-emerald-600">+{totalAdd}</span>
          <span className="text-red-600 ml-1.5">−{totalDel}</span>
        </span>
      </button>
      {expanded && (
        <div className="divide-y divide-surface-border/50">
          {files.map((f) => {
            const st = STATUS[f.status]
            const dir = dirname(f.path)
            const isOpen = openPath === f.path
            return (
              <button
                key={f.path}
                onClick={() => openDiff(checkpointId, label, f.path)}
                className={`w-full flex items-center gap-2 px-2.5 py-1.5 text-left transition-colors ${
                  isOpen ? 'bg-accent/15' : 'hover:bg-surface-raised'
                }`}
                title={isOpen ? t('changedFiles.closePreview', { path: f.path }) : t('changedFiles.fileStatus', { status: t(st.labelKey), path: f.path })}
              >
                <span className={`inline-flex items-center justify-center w-4 h-4 rounded border text-[11px] font-mono flex-shrink-0 ${st.cls}`}>
                  {st.glyph}
                </span>
                <span className="text-xs font-mono text-ink-soft truncate">{basename(f.path)}</span>
                {dir && (
                  <span className="text-[10px] font-mono text-ink-faint/60 truncate min-w-0">{dir}</span>
                )}
                <span className="ml-auto text-[10px] font-mono flex-shrink-0">
                  {f.additions > 0 && <span className="text-emerald-600">+{f.additions}</span>}
                  {f.deletions > 0 && <span className="text-red-600 ml-1">−{f.deletions}</span>}
                </span>
              </button>
            )
          })}
        </div>
      )}
    </div>
  )
}
