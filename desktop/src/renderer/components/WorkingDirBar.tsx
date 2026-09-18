import React, { useMemo, useState } from 'react'
import { useStore } from '../stores'
import { trustSummary } from '../../shared/trust-rules'
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
  const trust = useStore((s) => s.activeTrust)
  const answerTrust = useStore((s) => s.answerTrust)
  const [trustBusy, setTrustBusy] = useState(false)

  const handleChangeDir = async () => {
    const dir = await window.electronAPI.selectDirectory()
    if (dir) {
      setWorkingDir(dir)
    }
  }

  // The one place that always says whether this directory is trusted.
  //
  // The trust CARD only appears when the directory ships config to withhold, and
  // the panel banner shares that condition — so a brand-new directory used to be
  // silently untrusted: no prompt, no badge, and "always allow" refused with
  // nothing on screen explaining why. A recorded denial shows here too, so the
  // state is visible whichever way it was reached; trusting from here is the
  // explicit, deliberate way back.
  const untrusted = !!trust && trust.status !== 'trusted'
  const withheld = trustSummary(trust)

  const handleTrust = async () => {
    setTrustBusy(true)
    try {
      await answerTrust('trusted', true)
    } finally {
      setTrustBusy(false)
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
      {untrusted && (
        <button
          onClick={handleTrust}
          disabled={isStreaming || trustBusy}
          title={withheld
            ? t('trust.badgeHintConfig', { what: withheld })
            : t('trust.badgeHintBare')}
          className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/15 text-amber-700 hover:bg-amber-500/25 disabled:opacity-50 flex-shrink-0 transition-colors"
        >{t('trust.badge')}</button>
      )}
      <button
        onClick={handleChangeDir}
        disabled={isStreaming}
        className="text-xs text-ink-faint hover:text-accent disabled:opacity-50 flex-shrink-0 transition-colors"
      >{t('chat.changeDir')}</button>
    </div>
  )
}
