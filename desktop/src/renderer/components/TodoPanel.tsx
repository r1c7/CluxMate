import React, { useState } from 'react'
import { useStore } from '../stores'
import type { TodoItem } from '../../shared/types'
import { useT } from '../useI18n'

// Status glyph for one todo row: completed = check-in-ring, in_progress =
// spinning arc, pending = dashed ring. Mirrors the row semantics of the
// session-log todo/write fold (the whole list replaces on every update).
function StatusGlyph({ status }: { status: TodoItem['status'] }) {
  if (status === 'completed') {
    return (
      <svg className="w-3.5 h-3.5 text-emerald-500 flex-shrink-0" viewBox="0 0 14 14" fill="none" aria-hidden="true">
        <circle cx="7" cy="7" r="6.4" stroke="currentColor" strokeWidth="1.2" />
        <path
          d="M10.96 5.71 7.7 8.98a1.66 1.66 0 0 1-2.35 0L3.03 6.65l.93-.93 2.32 2.33 3.26-3.26.42.92Z"
          fill="currentColor"
        />
      </svg>
    )
  }
  if (status === 'in_progress') {
    return (
      <svg className="w-3.5 h-3.5 text-accent animate-spin flex-shrink-0" viewBox="0 0 14 14" fill="none" aria-hidden="true">
        <circle cx="7" cy="7" r="6.4" stroke="currentColor" strokeWidth="1.2" strokeDasharray="10 30" strokeLinecap="round" />
      </svg>
    )
  }
  return (
    <svg className="w-3.5 h-3.5 text-ink-faint flex-shrink-0" viewBox="0 0 14 14" fill="none" aria-hidden="true">
      <circle cx="7" cy="7" r="6.4" stroke="currentColor" strokeWidth="1.2" strokeDasharray="2.4 2.4" />
    </svg>
  )
}

// The plan strip: a raised, accent-edged card above the chat that clearly
// reads as a plan widget rather than part of the transcript — inset margins,
// rounded corners, shadow, and per-status colored progress counts. Collapsed
// by default so a long plan never pushes the conversation down. Null/empty
// lists render nothing (the store clears the list at each turn_start).
export default function TodoPanel() {
  const t = useT()
  const todos = useStore((s) => s.todos)
  const [collapsed, setCollapsed] = useState(true)

  if (!todos || todos.length === 0) return null

  const counts = {
    completed: todos.filter((i) => i.status === 'completed').length,
    in_progress: todos.filter((i) => i.status === 'in_progress').length,
    pending: todos.filter((i) => i.status === 'pending').length,
  }
  // Per-status colored segments; zero-count segments are omitted as noise.
  const segments: { key: string; label: string; cls: string }[] = []
  if (counts.completed > 0) {
    segments.push({ key: 'done', label: t('todo.progress.done', { done: counts.completed }), cls: 'text-emerald-600' })
  }
  if (counts.in_progress > 0) {
    segments.push({ key: 'active', label: t('todo.progress.active', { active: counts.in_progress }), cls: 'text-accent' })
  }
  if (counts.pending > 0) {
    segments.push({ key: 'pending', label: t('todo.progress.pending', { pending: counts.pending }), cls: 'text-ink-faint' })
  }

  return (
    <section
      data-testid="todo-panel"
      aria-label={t('todo.title')}
      className="mx-3 mt-2 rounded-xl border border-surface-border border-l-2 border-l-accent bg-chat-agent shadow-sm overflow-hidden flex-shrink-0"
    >
      <button
        type="button"
        onClick={() => setCollapsed((v) => !v)}
        aria-expanded={!collapsed}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-surface-raised/60 transition-colors"
      >
        <svg className="w-3.5 h-3.5 text-accent flex-shrink-0" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <rect x="2.6" y="2.6" width="8.8" height="8.8" rx="1.6" />
          <path d="m5 7.2 1.4 1.4L9 5.8" />
        </svg>
        <span className="text-xs font-semibold text-ink-soft flex-shrink-0">{t('todo.title')}</span>
        <span className="text-xs flex-1 min-w-0 truncate flex items-center gap-1">
          {segments.map((s, i) => (
            <React.Fragment key={s.key}>
              {i > 0 && <span className="text-ink-faint/60">·</span>}
              <span className={s.cls}>{s.label}</span>
            </React.Fragment>
          ))}
        </span>
        <span className={`text-ink-faint text-[10px] flex-shrink-0 transition-transform ${collapsed ? '' : 'rotate-180'}`}>▾</span>
      </button>
      {!collapsed && (
        <ul className="px-3 pt-1 pb-1.5 space-y-0.5 border-t border-surface-border">
          {todos.map((item) => (
            <li
              key={item.content}
              data-status={item.status}
              className="flex items-center gap-2 text-xs"
            >
              <StatusGlyph status={item.status} />
              <span
                className={`min-w-0 truncate ${
                  item.status === 'completed' ? 'text-ink-faint line-through' : 'text-ink-soft'
                }`}
              >
                {item.content}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
