import React from 'react'
import { useStore } from '../stores'
import { useT } from '../useI18n'

// Shown at the top of the project-config panels (hooks / MCP / skills) when the
// active session's directory is untrusted: editing these files is allowed (the
// user's hand), but nothing here is in force — say so instead of letting the
// panel look broken.
export default function TrustBanner() {
  const t = useT()
  const trust = useStore((s) => s.activeTrust)
  const answer = useStore((s) => s.answerTrust)
  if (!trust || trust.status === 'trusted' || trust.findings.length === 0) return null
  return (
    <div className="flex items-center gap-2 px-3 py-2 mb-2 rounded-lg border border-surface-border bg-surface-raised text-xs text-ink-soft">
      <span className="flex-1">{t('trust.banner')}</span>
      <button
        onClick={() => answer('trusted', true)}
        className="px-2.5 py-1 rounded bg-accent hover:bg-accent-hover text-accent-ink font-medium"
      >{t('trust.trustButton')}</button>
    </div>
  )
}
