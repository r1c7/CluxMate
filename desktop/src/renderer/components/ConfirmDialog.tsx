import React from 'react'
import { useStore } from '../stores'
import type { ConfirmRequest } from '../stores'
import { DialogButton, DialogShell } from './Dialog'
import { useT } from '../useI18n'

// Cap on the file list rendered in the body: enough to recognise the change,
// bounded so a huge dirty tree cannot push the buttons off screen. The same bound
// the two rich prompts use.
const MAX_LISTED_FILES = 30

// The app's ONE plain confirmation — the in-app replacement for `window.confirm`.
//
// It is driven entirely by the store (`confirm(request)` returns a promise that
// resolves true/false), so a call site reads exactly like the native one it
// replaces while the prompt itself is the same modal the delete-worktree and
// delete-group prompts are: same panel, same buttons, dark-mode-aware, and room
// for the FACTS a destructive confirm should show — a path, a branch, the files
// that would be lost — instead of a `\n`-joined OS message box.
//
// Focus lands on the safe exit (Cancel), so Enter cancels rather than destroys;
// Escape cancels too (DialogShell).
export default function ConfirmDialog({ request }: { request: ConfirmRequest }) {
  const t = useT()
  const resolveConfirm = useStore((s) => s.resolveConfirm)
  const details = request.details ?? []
  const files = request.files?.items ?? []

  return (
    <DialogShell
      title={request.title}
      width="w-[460px]"
      closeLabel={t('common.close')}
      onClose={() => resolveConfirm(false)}
      footer={(
        <>
          <DialogButton label={request.cancelLabel || t('common.cancel')} onClick={() => resolveConfirm(false)} autoFocus />
          <DialogButton
            tone={request.tone ?? 'danger'}
            label={request.confirmLabel || t('common.confirm')}
            onClick={() => resolveConfirm(true)}
          />
        </>
      )}
    >
      {request.body && <p className="text-sm text-ink-soft leading-relaxed">{request.body}</p>}

      {details.length > 0 && (
        <div className="rounded-lg border border-surface-border px-3 py-2 space-y-1">
          {details.map((d) => (
            <div key={d.label} className="flex items-baseline gap-3 text-xs">
              <span className="text-ink-faint flex-shrink-0">{d.label}</span>
              <span className={`text-ink truncate ${d.mono ? 'font-mono' : ''}`} title={d.value}>{d.value}</span>
            </div>
          ))}
        </div>
      )}

      {request.files && (request.files.items.length === 0 ? (
        <div className="text-[11px] text-ink-faint">{request.files.label}</div>
      ) : (
        <div>
          <div className="text-[11px] text-ink-soft mb-1">{request.files.label}</div>
          <pre className="max-h-32 overflow-y-auto rounded-lg border border-surface-border px-3 py-2 text-[11px] font-mono text-ink-soft whitespace-pre-wrap break-words">
            {files.slice(0, MAX_LISTED_FILES).join('\n') + (files.length > MAX_LISTED_FILES ? '\n…' : '')}
          </pre>
        </div>
      ))}

      {request.note && (
        <div className="text-[11px] text-ink-faint leading-snug">{request.note}</div>
      )}
    </DialogShell>
  )
}
