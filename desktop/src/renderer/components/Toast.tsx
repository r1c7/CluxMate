import React, { useEffect } from 'react'
import { useStore } from '../stores'
import { useT } from '../useI18n'

// Global toast bound to the store's `error` channel. Until now `error` was set
// by many actions (session load, restore, MCP, and the cross-session undo
// conflict warning) but never rendered anywhere — so every one of those messages
// was silent. This surfaces them as a dismissible banner.
export default function Toast() {
  const t = useT()
  const error = useStore((s) => s.error)
  const clearError = useStore((s) => s.clearError)

  // Auto-dismiss after a while so a stale message doesn't linger. Re-armed
  // whenever the message changes.
  useEffect(() => {
    if (!error) return
    const t = setTimeout(clearError, 8000)
    return () => clearTimeout(t)
  }, [error, clearError])

  if (!error) return null

  // Centered over the chat area. A click-through overlay (pointer-events-none)
  // keeps the rest of the UI usable; only the card itself is interactive.
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center pointer-events-none">
      <div className="pointer-events-auto max-w-lg w-[min(92vw,32rem)] bg-surface-raised border border-red-500/40 rounded-lg shadow-2xl px-4 py-3 flex items-start gap-3">
        <span className="mt-0.5 w-2 h-2 rounded-full bg-red-500 flex-shrink-0" />
        <p className="flex-1 text-sm text-ink leading-relaxed break-words">{error}</p>
        <button
          onClick={clearError}
          className="text-ink-faint hover:text-ink text-lg leading-none flex-shrink-0 -mt-0.5"
          title={t('common.dismiss')}
        >×</button>
      </div>
    </div>
  )
}
