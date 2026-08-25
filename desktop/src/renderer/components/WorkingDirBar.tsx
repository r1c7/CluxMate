import React, { useMemo } from 'react'
import { useStore } from '../stores'
import { useT } from '../useI18n'

// Keep the folders closest to the working directory's basename visible when the
// absolute prefix is long, collapsing the head into a leading ellipsis. The tail
// is the useful part — the root/drive rarely matters during a session.
const MAX_PARENT_LEN = 40

function splitPath(p: string): { parent: string; base: string; sep: string } {
  const norm = (p || '').replace(/[\\/]+$/, '')
  if (!norm) return { parent: '', base: '', sep: '/' }
  const idx = Math.max(norm.lastIndexOf('\\'), norm.lastIndexOf('/'))
  const sep = norm.includes('\\') ? '\\' : '/'
  if (idx === -1) return { parent: '', base: norm, sep }
  return { parent: norm.slice(0, idx), base: norm.slice(idx + 1), sep }
}

function headTruncate(s: string, sep: string, maxLen: number): string {
  if (s.length <= maxLen) return s
  const segments = s.split(sep).filter(Boolean)
  let out = segments[segments.length - 1] ?? ''
  for (let i = segments.length - 2; i >= 0; i--) {
    if (out.length + segments[i].length + 1 > maxLen) return '…' + sep + out
    out = segments[i] + sep + out
  }
  return out
}

export default function WorkingDirBar() {
  const t = useT()
  const workingDir = useStore((s) => s.workingDir)
  const setWorkingDir = useStore((s) => s.setWorkingDir)
  const isStreaming = useStore((s) => s.isStreaming)

  const handleChangeDir = async () => {
    const dir = await window.electronAPI.selectDirectory()
    if (dir) {
      setWorkingDir(dir)
    }
  }

  const { parent, base, sep } = useMemo(() => {
    const s = splitPath(workingDir)
    return { ...s, parent: s.parent ? headTruncate(s.parent, s.sep, MAX_PARENT_LEN) : '' }
  }, [workingDir])

  return (
    <div className="border-t border-surface-border px-3 py-1.5 bg-sidebar flex items-center gap-2">
      <span className="text-xs text-ink-faint flex-shrink-0">{t('chat.workingDir')}</span>
      <button
        onClick={handleChangeDir}
        title={workingDir}
        className="flex-1 min-w-0 flex items-baseline gap-0.5 text-left group"
      >
        {parent && (
          <span className="text-xs text-ink-faint font-mono truncate opacity-80">
            {parent}{sep}
          </span>
        )}
        <span className="text-xs text-ink font-mono font-medium flex-shrink-0 group-hover:text-accent transition-colors">
          {base || '...'}
        </span>
      </button>
      <button
        onClick={handleChangeDir}
        disabled={isStreaming}
        className="text-xs text-ink-faint hover:text-accent disabled:opacity-50 flex-shrink-0 transition-colors"
      >{t('chat.changeDir')}</button>
    </div>
  )
}
