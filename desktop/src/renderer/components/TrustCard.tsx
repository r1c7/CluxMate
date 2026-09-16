import React from 'react'
import { useStore } from '../stores'
import { trustSummary } from '../../shared/trust-rules'
import { useT } from '../useI18n'

// Bottom-docked card asking whether this directory's project config may be
// loaded. Same shell as QuestionCard/PermissionCard: it replaces the composer,
// never blocks the message list, and only the user can dismiss it.
export default function TrustCard() {
  const t = useT()
  const pending = useStore((s) => s.pendingTrust)
  const answer = useStore((s) => s.answerTrust)
  if (!pending) return null

  return (
    <div className="border-t border-surface-border bg-chat pl-14 pr-4 py-3 shrink-0">
      <div className="rounded-xl border border-surface-border bg-chat-agent shadow-md px-4 py-3">
        <div className="text-sm font-semibold text-ink mb-1">{t('trust.title')}</div>
        <div className="text-sm text-ink-soft mb-2">{t('trust.body')}</div>
        <ul className="mb-3 space-y-1">
          {pending.findings.map((f) => (
            <li key={f.kind} className="text-xs text-ink-faint">
              <span className="text-ink-soft">{f.label}</span> — {f.path}
            </li>
          ))}
        </ul>
        <div className="text-xs text-ink-faint mb-3">{trustSummary(pending)}</div>
        <div className="flex items-center gap-2 justify-end">
          <button onClick={() => answer('denied', true)}
            className="px-4 py-1.5 bg-transparent hover:bg-surface-border text-ink-soft text-sm rounded-lg font-medium transition-colors">
            {t('trust.deny')}
          </button>
          <button onClick={() => answer('trusted', false)}
            className="px-4 py-1.5 border border-surface-border hover:border-accent/50 text-ink-soft text-sm rounded-lg font-medium transition-colors">
            {t('trust.session')}
          </button>
          <button onClick={() => answer('trusted', true)}
            className="px-4 py-1.5 bg-accent hover:bg-accent-hover text-accent-ink text-sm rounded-lg font-medium transition-colors">
            {t('trust.remember')}
          </button>
        </div>
      </div>
    </div>
  )
}
