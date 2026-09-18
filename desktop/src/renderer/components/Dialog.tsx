import React, { useEffect } from 'react'

// ── the ONE dialog chrome ─────────────────────────────────────────────────────
//
// Every confirmation in the app is built from these three pieces, so they cannot
// drift apart again: the two rich prompts (DeleteWorktreeSessionDialog,
// DeleteGroupDialog) and the plain ConfirmDialog all render this shell, these
// buttons and this warning block. What the app used to have was three different
// things — two in-app modals plus a handful of `window.confirm` calls, which are
// unstyled OS dialogs with a different layout, no dark-mode-aware colours and no
// room for the facts a destructive action should show (a path, a branch, the
// files that would be lost).
//
// The tokens are the ones DeleteWorktreeSessionDialog was written with and are
// deliberately unchanged — it is the reference the rest now follows.
//
// `DialogShell` also owns the two behaviours every dialog wants and none had:
// Escape closes it, and `closeDisabled` is what a caller sets while an action is
// mid-flight (closing then would unmount the component that reports the refusal,
// and the user would never learn that nothing happened).

export function DialogShell({
  title,
  onClose,
  closeLabel,
  closeDisabled = false,
  width = 'w-[520px]',
  subtitle,
  footerLeading,
  footer,
  children,
}: {
  title: string
  onClose: () => void
  /** Accessible name for the × — the caller's own dictionary entry. */
  closeLabel?: string
  /** True while an action is running: the ×, Escape and the footer are all held. */
  closeDisabled?: boolean
  /** Panel width class; the default matches the reference prompt. */
  width?: string
  /** Small line under the header (a path, an identity) — omitted when empty. */
  subtitle?: string
  /** Footer content pinned to the left (a spinner or a hint). */
  footerLeading?: React.ReactNode
  footer: React.ReactNode
  children: React.ReactNode
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !closeDisabled) onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [closeDisabled, onClose])

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={`bg-chat-agent rounded-xl ${width} max-h-[85vh] flex flex-col shadow-2xl border border-surface-border`}
      >
        <div className="flex items-center justify-between px-6 pt-5 pb-2">
          <h2 className="text-base font-semibold text-ink">{title}</h2>
          <button
            type="button"
            onClick={onClose}
            disabled={closeDisabled}
            aria-label={closeLabel}
            className="text-ink-faint hover:text-ink text-xl disabled:opacity-50"
          >&times;</button>
        </div>
        {subtitle && (
          <div className="px-6 pb-2 text-[11px] text-ink-faint/70 truncate" title={subtitle}>{subtitle}</div>
        )}

        {/* min-h-0 on the scroll body: a flex item's automatic minimum size is its
            content height, so without it a long list (dirty files, worktrees)
            would grow the panel past max-h and be clipped instead of scrolling. */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-3 space-y-4">{children}</div>

        <div className="px-6 py-4 border-t border-surface-border flex flex-wrap items-center justify-end gap-2">
          {footerLeading}
          {footer}
        </div>
      </div>
    </div>
  )
}

// The three button roles every dialog here needs. `primary` is the app's normal
// affirmative action (the same accent fill the sidebar's "New Session" uses),
// `danger` the bordered red the destructive exits already used, `ghost` the
// neutral cancel/second choice.
export function DialogButton({
  label,
  onClick,
  tone = 'ghost',
  disabled = false,
  title,
  autoFocus = false,
}: {
  label: string
  onClick: () => void
  tone?: 'ghost' | 'danger' | 'primary'
  disabled?: boolean
  title?: string
  /** Focus on open. A destructive prompt focuses its safe exit, never the one
   *  that deletes something — Enter then cancels instead of confirming. */
  autoFocus?: boolean
}) {
  const cls = tone === 'danger'
    ? 'border border-red-500/40 bg-red-500/5 hover:bg-red-500/10 text-red-600 font-medium'
    : tone === 'primary'
      // The accent fill the app's affirmative actions already used, with the
      // same border width as the other two roles so a row of them lines up.
      ? 'border border-transparent bg-accent hover:bg-accent-hover disabled:bg-surface-raised text-accent-ink disabled:text-ink-faint font-medium'
      : 'bg-surface-raised hover:bg-sidebar-hover text-ink border border-surface-border'
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      autoFocus={autoFocus}
      className={`px-4 py-2 text-sm rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors ${cls}`}
    >{label}</button>
  )
}

// The one warning block: `error` for "this failed", `warn` for "this is a
// deliberate refusal / something to know" — the distinction the delete prompts
// rely on ("nothing was deleted" must not read as "the delete broke").
export function Warning({ tone, children }: { tone: 'warn' | 'error'; children: React.ReactNode }) {
  const cls = tone === 'error'
    ? 'border-red-500/40 bg-red-500/5 text-red-600'
    : 'border-amber-500/40 bg-amber-500/5 text-amber-700'
  return (
    <div className={`rounded-lg border px-3 py-2 text-xs leading-relaxed ${cls}`}>{children}</div>
  )
}
