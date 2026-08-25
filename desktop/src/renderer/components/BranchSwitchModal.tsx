import React, { useState } from 'react'
import { useStore } from '../stores'
import type { GitCheckoutStrategy } from '../../shared/types'
import { useT } from '../useI18n'
import { tGlobal } from '../i18n'

interface Props {
  branch: string
  onClose: () => void
}

// Modal shown when the user picks a different branch while the current one has
// uncommitted changes. Each reconciliation strategy is a full-width row (title +
// description) so the consequence of each is clear; Discard is visually marked
// destructive and Cancel sits as its own footer action.
export default function BranchSwitchModal({ branch, onClose }: Props) {
  const t = useT()
  const workingDir = useStore((s) => s.workingDir)
  const refreshGitInfo = useStore((s) => s.refreshGitInfo)
  const setError = useStore((s) => s.setError)
  const [busy, setBusy] = useState(false)

  const run = async (strategy: GitCheckoutStrategy) => {
    setBusy(true)
    try {
      const res = await window.electronAPI.checkoutBranch(workingDir, branch, strategy)
      if (res.ok) {
        await refreshGitInfo()
        onClose()
      } else {
        setError(t('git.switchFailed', { msg: res.message || tGlobal('error.unknown') }))
        onClose()
      }
    } catch (e: any) {
      setError(t('git.switchFailed', { msg: e?.message || tGlobal('error.unknown') }))
      onClose()
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-chat-agent rounded-xl w-[420px] p-6 shadow-2xl border border-surface-border">
        <div className="flex items-center justify-between mb-3">
          <h2 className="text-base font-semibold text-ink">{t('branch.switchTitle')}</h2>
          <button onClick={onClose} className="text-ink-faint hover:text-ink text-xl">&times;</button>
        </div>

        <p className="text-sm text-ink-soft leading-relaxed">
          {t('branch.prompt', { branch })}
        </p>

        <div className="mt-4 space-y-2">
          <Option
            title={t('branch.stash')}
            description={t('branch.stashDesc')}
            onClick={() => run('stash')}
            disabled={busy}
          />
          <Option
            title={t('branch.commit')}
            description={t('branch.commitDesc')}
            onClick={() => run('commit')}
            disabled={busy}
          />
          <Option
            title={t('branch.discard')}
            description={t('branch.discardDesc')}
            onClick={() => run('discard')}
            disabled={busy}
            destructive
          />
        </div>

        <div className="mt-5 flex justify-end gap-2 border-t border-surface-border pt-4">
          <button
            onClick={onClose}
            disabled={busy}
            className="px-4 py-2 bg-surface-raised hover:bg-sidebar-hover text-ink text-sm rounded-lg border border-surface-border disabled:opacity-50"
          >{t('branch.cancel')}</button>
        </div>
      </div>
    </div>
  )
}

function Option({ title, description, onClick, disabled, destructive }: {
  title: string
  description: string
  onClick: () => void
  disabled?: boolean
  destructive?: boolean
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={`w-full text-left px-3 py-2.5 rounded-lg border transition-colors disabled:opacity-50 ${
        destructive
          ? 'border-red-500/40 bg-red-500/5 hover:bg-red-500/10'
          : 'border-surface-border bg-surface-raised hover:bg-sidebar-hover'
      }`}
    >
      <span className={`block text-sm ${destructive ? 'text-red-600' : 'text-ink'}`}>{title}</span>
      <span className="block text-xs text-ink-faint mt-0.5 leading-snug">{description}</span>
    </button>
  )
}
