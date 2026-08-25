import React, { useState, useRef, useCallback, useEffect } from 'react'
import { useStore } from '../stores'
import type { SessionMeta, GroupMeta, SessionSearchHit } from '../../shared/types'
import { useT } from '../useI18n'

// ── Highlight ──
// Split `text` on every case-insensitive occurrence of `query` and wrap the
// matched spans in a highlight. Non-matching runs render as-is.
function Highlight({ text, query }: { text: string; query: string }) {
  const q = query.trim()
  if (!q) return <>{text}</>
  const lower = text.toLowerCase()
  const ql = q.toLowerCase()
  const parts: { str: string; hit: boolean }[] = []
  let from = 0
  while (true) {
    const idx = lower.indexOf(ql, from)
    if (idx === -1) { parts.push({ str: text.slice(from), hit: false }); break }
    if (idx > from) parts.push({ str: text.slice(from, idx), hit: false })
    parts.push({ str: text.slice(idx, idx + ql.length), hit: true })
    from = idx + ql.length
  }
  return (
    <>
      {parts.map((p, i) =>
        p.hit
          ? <mark key={i} className="bg-accent/25 text-ink rounded-[3px] px-0.5">{p.str}</mark>
          : <span key={i}>{p.str}</span>
      )}
    </>
  )
}

// ── InlineEdit ──
function InlineEdit({
  value, onSave, onCancel,
}: {
  value: string; onSave: (v: string) => void; onCancel: () => void
}) {
  const ref = useRef<HTMLInputElement>(null)
  React.useEffect(() => {
    ref.current?.focus()
    ref.current?.select()
  }, [])
  const commit = () => {
    const v = ref.current?.value.trim() || value
    onSave(v)
  }
  return (
    <input
      ref={ref}
      defaultValue={value}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') commit()
        if (e.key === 'Escape') onCancel()
      }}
      onClick={(e) => e.stopPropagation()}
      className="flex-1 min-w-0 bg-surface-input rounded px-1 py-0.5 text-[13px] text-ink outline-none ring-1 ring-accent"
    />
  )
}

/* ── Chevron ──*/
function Chevron({ open, isAuto }: { open: boolean; isAuto?: boolean }) {
  // Auto (project) groups use open/closed folder glyphs so the collapse state
  // is obvious at a glance; manual groups use a chevron that rotates down.
  if (isAuto) {
    return (
      <svg
        className="w-3.5 h-3.5 flex-shrink-0 text-ink-soft"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {open ? (
          <path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2" />
        ) : (
          <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
        )}
      </svg>
    )
  }
  return (
    <span
      className={`text-[11px] text-ink-faint/60 w-3.5 text-center flex-shrink-0 leading-none select-none transition-transform ${
        open ? 'rotate-90' : ''
      }`}
    >
      ▶
    </span>
  )
}

// ── Move-rule helpers ──
// Project auto-groups are keyed by their resolved path (mirrors the backend
// `_ensureGroupForCwd`). Drop-target matching uses the normalized path, not the
// basename, so two same-named directories don't collide.

// ── Project-name disambiguation ──
// Two different directories can share a basename (e.g. C:\a\foo and D:\b\foo).
// Auto groups are keyed by their resolved path now, so they no longer collapse —
// but their labels would still be identical. Resolve that by showing the shortest
// unique path suffix (parent/basename, grandparent/parent/basename, …) only for
// the names that actually collide; unique basenames render as-is.
function normPath(p: string): string {
  return p.replace(/\\/g, '/').replace(/\/+$/, '')
}

function disambiguateProjectLabels(groups: GroupMeta[]): Map<string, string> {
  const labels = new Map<string, string>()
  const autos = groups.filter((g) => g.is_auto && g.path)
  const byName = new Map<string, GroupMeta[]>()
  for (const g of autos) {
    const list = byName.get(g.name) || []
    list.push(g)
    byName.set(g.name, list)
  }
  for (const g of autos) {
    const conflict = byName.get(g.name) || []
    if (conflict.length <= 1) { labels.set(g.id, g.name); continue }
    const parts = normPath(g.path!).split('/')
    let label = g.name
    for (let k = 2; k <= parts.length; k++) {
      const suffix = parts.slice(-k).join('/')
      const clash = conflict.some((other) =>
        other.id !== g.id && normPath(other.path!).split('/').slice(-k).join('/') === suffix
      )
      if (!clash) { label = suffix; break }
    }
    labels.set(g.id, label)
  }
  return labels
}

// Move rules:
//   - A session in a Project (auto group) or ungrouped can only move into a
//     user-created Group.
//   - A session in a user-created Group can only move back to its Project (the
//     auto group named after its cwd).
function canMoveSessionTo(groups: GroupMeta[], session: SessionMeta, targetGroupId: string | null): boolean {
  const current = groups.find((g) => g.id === session.group_id)
  if (current && !current.is_auto) {
    if (targetGroupId === null) return false
    const target = groups.find((g) => g.id === targetGroupId)
    // Match the session's own project by its resolved path (not basename), so
    // two same-named directories don't both light up as the drop target.
    return !!target && target.is_auto && normPath(target.path || '') === normPath(session.cwd)
  }
  if (targetGroupId === null) return false
  const target = groups.find((g) => g.id === targetGroupId)
  return !!target && !target.is_auto
}

// ── SessionItem ──
function SessionItem({
  session, active, onClick, onDelete, onRename, onContextMenu,
  editing, onStartEdit, onCancelEdit, showCwd,
}: {
  session: SessionMeta
  active: boolean
  onClick: () => void
  onDelete: () => void
  onRename: (title: string) => void
  onContextMenu?: (e: React.MouseEvent) => void
  editing: boolean
  onStartEdit: () => void
  onCancelEdit: () => void
  // Auto groups (Projects) already convey the working directory via their group
  // name, so the per-session cwd line is only shown inside user-created groups
  // (and ungrouped sessions), where the location isn't otherwise visible.
  showCwd: boolean
}) {
  const t = useT()
  const isStreaming = useStore((s) => s.sessionStates.get(session.id)?.isStreaming)
  const hasUnread = useStore((s) => s.sessionStates.get(session.id)?.hasUnread)
  // Which kind of human input this session is blocked on, if any. Distinct from
  // "streaming": the turn is paused on a decision (tool approval, diff review,
  // or a question answer), not actively generating. A stable primitive return
  // keeps Zustand's selector equality from re-rendering unless the block type
  // actually changes.
  const pending = useStore((s) => {
    const st = s.sessionStates.get(session.id)
    if (!st) return null
    if (st.pendingQuestion) return 'question' as const
    if (st.pendingPermission) return 'permission' as const
    if (st.pendingBatchEdit) return 'edit' as const
    return null
  })
  const pendingTitle =
    pending === 'question' ? t('sessionList.waitingAnswer')
    : pending === 'permission' ? t('sessionList.waitingApproval')
    : pending === 'edit' ? t('sessionList.waitingReview')
    : undefined

  if (editing) {
    return (
      <div className="px-3 py-1.5 flex items-center gap-1">
        <InlineEdit
          value={session.title}
          onSave={(v) => { onRename(v); onCancelEdit() }}
          onCancel={onCancelEdit}
        />
      </div>
    )
  }

  return (
    <div
      onClick={onClick}
      onDoubleClick={onStartEdit}
      onContextMenu={onContextMenu}
      className={`px-3 py-2 cursor-pointer group flex items-center border-l-[3px] transition-colors ${
        active
          ? 'bg-accent-muted text-ink border-accent'
          : 'text-ink-soft border-transparent hover:bg-sidebar-hover hover:text-ink'
      }`}
    >
      <div className="min-w-0 flex-1">
        {/* NOTE: no `truncate`/overflow-hidden here — it would clip the status
            badge's expanding ping halo at the row's left edge. Truncation stays
            on the title span below (its own overflow:hidden also lets it shrink
            as a flex item). */}
        <div className="text-[13px] flex items-center gap-1.5 leading-snug">
          {/* Status indicator, in a fixed-width (16px) slot so titles stay
              aligned across sessions regardless of state:
                - needs input: an amber "!" badge — the turn is paused on a
                  human decision (tool approval, diff review, or a question),
                  NOT generating. Rendered in place of the spinner so a session
                  silently waiting on approval can't be mistaken for one that's
                  still working.
                - working: eight green dots chasing clockwise around a circle —
                  the familiar "generating / typing" signal while the turn streams.
                - unread:  a single solid green dot — a turn finished while this
                  session was in the background and hasn't been viewed yet.
                - otherwise (idle / connected / off): nothing. */}
          {pending ? (
            <span className="relative inline-flex items-center justify-center w-4 h-4 flex-shrink-0" title={pendingTitle}>
              <span className="absolute inline-flex w-3 h-3 rounded-full bg-amber-400 session-ping" />
              <span className="relative inline-flex w-3.5 h-3.5 rounded-full bg-amber-400 text-amber-950 items-center justify-center">
                <span className="text-[10px] font-bold leading-none">!</span>
              </span>
            </span>
          ) : isStreaming ? (
            <span className="relative inline-block w-4 h-4 flex-shrink-0" title={t('sessionList.agentWorking')}>
              {Array.from({ length: 8 }).map((_, i) => (
                <span
                  key={i}
                  className="session-spin-dot"
                  style={{
                    // Negative rotation angles lay the dots out counterclockwise
                    // (increasing i), so the brightness wave — which travels to
                    // DECREASING i — sweeps clockwise around the ring.
                    transform: `rotate(${-i * 45}deg) translateY(-5.5px)`,
                    animationDelay: `${-i * 0.15}s`,
                  }}
                />
              ))}
            </span>
          ) : hasUnread ? (
            <span className="inline-flex items-center w-4 flex-shrink-0" title={t('sessionList.newOutput')}>
              <span className="inline-block w-2 h-2 rounded-full bg-green-500" />
            </span>
          ) : (
            <span className="inline-block w-4 flex-shrink-0" aria-hidden />
          )}
          <span className="truncate">{session.title || t('sessionList.newSession')}</span>
        </div>
        {showCwd && (
          <div className="text-[11px] text-ink-faint/60 truncate mt-0.5" title={session.cwd}>
            {session.cwd}
          </div>
        )}
      </div>
      {/* Guard with !! — an unnormalized SQLite row can carry is_pinned = 0
          (number), and `0 && <svg/>` would render a literal "0". */}
      {!!session.is_pinned && (
        // SVG pin (lucide) instead of the 📌 emoji — the emoji renders as a
        // tiny faint blob at sidebar size and reads like a "0".
        <span className="flex-shrink-0 ml-1" title={t('sessionList.pinned')}>
          <svg
            className="w-3 h-3 text-amber-500"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12 17v5" />
            <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z" />
          </svg>
        </span>
      )}
      <button
        onClick={(e) => { e.stopPropagation(); onDelete() }}
        className="text-ink-faint/30 hover:text-red-500 opacity-0 group-hover:opacity-100 w-5 h-5 flex items-center justify-center rounded text-[13px] flex-shrink-0 transition-all"
        title={t('sessionList.deleteSession')}
      >&times;</button>
    </div>
  )
}

// ── CollapsibleGroup ──
function CollapsibleGroup({
  group, sessions, activeSessionId,
  onSessionClick, onSessionDelete, onSessionRename, onSessionContextMenu,
  onRenameGroup, onDeleteGroup, onGroupContextMenu,
  onDrop, dragOver,
  editingSessionId, editingGroupId,
  onStartEditSession, onCancelEditSession,
  onStartEditGroup, onCancelEditGroup,
  onCreateSession,
  displayName,
}: {
  group: GroupMeta
  sessions: SessionMeta[]
  activeSessionId: string | null
  onSessionClick: (id: string) => void
  onSessionDelete: (id: string) => void
  onSessionRename: (id: string, title: string) => void
  onSessionContextMenu?: (session: SessionMeta, groupId?: string | null) => (e: React.MouseEvent) => void
  onRenameGroup: (id: string, name: string) => void
  onDeleteGroup: (id: string) => void
  onGroupContextMenu?: (e: React.MouseEvent) => void
  onDrop: (e: React.DragEvent) => void
  dragOver: boolean
  editingSessionId: string | null
  editingGroupId: string | null
  onStartEditSession: (id: string) => void
  onCancelEditSession: () => void
  onStartEditGroup: (id: string) => void
  onCancelEditGroup: () => void
  // Auto (project) groups render a "+" that creates a session in this project.
  onCreateSession?: () => void
  // Disambiguated project label (shortest unique path suffix); falls back to name.
  displayName?: string
}) {
  const t = useT()
  const hasActive = sessions.some((s) => s.id === activeSessionId)
  const [collapsed, setCollapsed] = useState(!hasActive)
  // When the active session changes (e.g. initial load assigns one), if it lives
  // in this group, expand so the user sees it without clicking.
  useEffect(() => {
    if (hasActive) setCollapsed(false)
  }, [hasActive])

  // Auto groups (Projects) are named after the working directory's basename;
  // on hover reveal the full path(s) of the sessions they hold. (Two different
  // Auto groups are keyed by their resolved path; hover reveals it (falling back
  // to the sessions' cwd set for any pre-migration row).
  const groupTitle = group.is_auto
    ? (group.path || Array.from(new Set(sessions.map((s) => s.cwd))).join('\n'))
    : undefined

  return (
    <div
      onDragOver={(e) => e.preventDefault()}
      onDrop={onDrop}
    >
      <div
        onClick={() => setCollapsed(!collapsed)}
        onDoubleClick={(e) => { e.stopPropagation(); if (!group.is_auto) onStartEditGroup(group.id) }}
        onContextMenu={onGroupContextMenu}
        className={`px-3 py-1.5 flex items-center gap-1.5 cursor-pointer text-[11px] font-semibold tracking-wide text-ink-faint hover:text-ink transition-colors select-none rounded-sm ${
          dragOver ? 'bg-accent/10 ring-1 ring-inset ring-accent/30 text-accent' : ''
        }`}
      >
        <Chevron open={!collapsed} isAuto={!!group.is_auto} />
        {editingGroupId === group.id ? (
          <InlineEdit
            value={group.name}
            onSave={(v) => { onRenameGroup(group.id, v); onCancelEditGroup() }}
            onCancel={onCancelEditGroup}
          />
        ) : (
          <span className="flex-1 truncate" title={groupTitle}>{displayName ?? group.name}</span>
        )}
        {group.is_auto && onCreateSession && (
          <button
            onClick={(e) => { e.stopPropagation(); onCreateSession() }}
            className="text-ink-soft hover:text-accent hover:bg-accent/10 w-5 h-5 flex items-center justify-center rounded font-bold text-[15px] leading-none flex-shrink-0 transition-colors"
            title={t('sessionList.newSessionInProject')}
          >+</button>
        )}
      </div>

      {!collapsed && (
        <div className="pl-3">
          {sessions.length === 0 ? (
            <div className="px-3 py-2 text-[11px] text-ink-faint/40 italic">
              {t('sessionList.dragHere')}
            </div>
          ) : (
            sessions.map((s) => (
              <SessionItem
                key={s.id}
                session={s}
                active={s.id === activeSessionId}
                onClick={() => onSessionClick(s.id)}
                onDelete={() => onSessionDelete(s.id)}
                onRename={(title) => onSessionRename(s.id, title)}
                onContextMenu={onSessionContextMenu?.(s, group.id)}
                editing={editingSessionId === s.id}
                onStartEdit={() => onStartEditSession(s.id)}
                onCancelEdit={onCancelEditSession}
                showCwd={!group.is_auto}
              />
            ))
          )}
        </div>
      )}
    </div>
  )
}

// ── SearchResultItem ──
// A flat (non-grouped) hit row shown while a search is active. Reuses the
// visual language of SessionItem but shows match snippets under the title.
function SearchResultItem({
  hit, query, active, onClick, onDelete, onRename, onContextMenu,
}: {
  hit: SessionSearchHit
  query: string
  active: boolean
  onClick: () => void
  onDelete: () => void
  onRename: (title: string) => void
  onContextMenu?: (e: React.MouseEvent) => void
}) {
  const t = useT()
  const session = hit.meta
  const [editing, setEditing] = useState(false)
  return (
    <div
      onClick={onClick}
      onDoubleClick={() => setEditing(true)}
      onContextMenu={onContextMenu}
      className={`px-3 py-2 cursor-pointer group flex items-start border-l-[3px] transition-colors ${
        active
          ? 'bg-accent-muted text-ink border-accent'
          : 'text-ink-soft border-transparent hover:bg-sidebar-hover hover:text-ink'
      }`}
    >
      <div className="min-w-0 flex-1">
        {editing ? (
          <InlineEdit
            value={session.title}
            onSave={(v) => { onRename(v); setEditing(false) }}
            onCancel={() => setEditing(false)}
          />
        ) : (
          <div className="text-[13px] flex items-center gap-1.5 leading-snug">
            {!!session.is_pinned && (
              <svg className="w-3 h-3 text-amber-500 flex-shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 17v5" />
                <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z" />
              </svg>
            )}
            <span className="truncate"><Highlight text={session.title || t('sessionList.newSession')} query={query} /></span>
          </div>
        )}
        <div className="text-[11px] text-ink-faint/50 truncate mt-0.5">{session.cwd}</div>
        {hit.snippets.map((snip, i) => (
          <div key={i} className="text-[11px] text-ink-faint/70 mt-1 leading-snug break-words">
            <Highlight text={snip} query={query} />
          </div>
        ))}
      </div>
      <button
        onClick={(e) => { e.stopPropagation(); onDelete() }}
        className="text-ink-faint/30 hover:text-red-500 opacity-0 group-hover:opacity-100 w-5 h-5 flex items-center justify-center rounded text-[13px] flex-shrink-0 transition-all"
        title={t('sessionList.deleteSession')}
      >&times;</button>
    </div>
  )
}

// ── Main SessionList ──
export default function SessionList({ width }: { width: number }) {
  const t = useT()
  const sessions = useStore((s) => s.sessions)
  const groups = useStore((s) => s.groups)
  const activeSessionId = useStore((s) => s.activeSessionId)
  const searchQuery = useStore((s) => s.searchQuery)
  const searchResults = useStore((s) => s.searchResults)
  const setSearchQuery = useStore((s) => s.setSearchQuery)
  const clearSearch = useStore((s) => s.clearSearch)
  const mainView = useStore((s) => s.mainView)
  const switchSession = useStore((s) => s.switchSession)
  const deleteSession = useStore((s) => s.deleteSession)
  const createSession = useStore((s) => s.createSession)
  const createGroup = useStore((s) => s.createGroup)
  const renameGroup = useStore((s) => s.renameGroup)
  const deleteGroup = useStore((s) => s.deleteGroup)
  const moveSession = useStore((s) => s.moveSession)
  const renameSession = useStore((s) => s.renameSession)
  const showSkills = useStore((s) => s.showSkills)
  const showMcp = useStore((s) => s.showMcp)
  const showHooks = useStore((s) => s.showHooks)
  const showChat = useStore((s) => s.showChat)
  const openContextMenu = useStore((s) => s.openContextMenu)
  const editingSessionId = useStore((s) => s.editingSessionId)
  const editingGroupId = useStore((s) => s.editingGroupId)
  const startEditSession = useStore((s) => s.startEditSession)
  const cancelEditSession = useStore((s) => s.cancelEditSession)
  const startEditGroup = useStore((s) => s.startEditGroup)
  const cancelEditGroup = useStore((s) => s.cancelEditGroup)

  const [newGroupMode, setNewGroupMode] = useState(false)
  const newGroupRef = useRef<HTMLInputElement>(null)
  const [dragSessionId, setDragSessionId] = useState<string | null>(null)
  const [dragOverGroupId, setDragOverGroupId] = useState<string | null>(null)

  const sessionsByGroup = new Map<string | null, SessionMeta[]>()
  for (const s of sessions) {
    const key = s.group_id ?? '__root'
    if (!sessionsByGroup.has(key)) sessionsByGroup.set(key, [])
    sessionsByGroup.get(key)!.push(s)
  }
  const rootSessions = sessionsByGroup.get('__root') || []

  // Auto groups sharing a basename get a shortest-unique path suffix label.
  const projectLabels = disambiguateProjectLabels(groups)

  // Whether the currently dragged session may be dropped onto the given group
  // (or the root). Used to gate the drop highlight so invalid targets never
  // look droppable.
  const canDropTo = (groupId: string | null): boolean => {
    if (!dragSessionId) return false
    const session = sessions.find((s) => s.id === dragSessionId)
    return !!session && canMoveSessionTo(groups, session, groupId)
  }

  const onNewGroupSubmit = () => {
    const name = newGroupRef.current?.value.trim()
    if (name) createGroup(name)
    setNewGroupMode(false)
  }

  const handleDropOnRoot = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    setDragOverGroupId(null)
    // Moving to the root ("no group") is no longer a valid target — a session
    // lives in either a Project or a Group, so a drop here is a no-op.
  }, [])

  const handleDropOnGroup = useCallback((groupId: string) => (e: React.DragEvent) => {
    e.preventDefault()
    setDragOverGroupId(null)
    if (!dragSessionId) return
    const session = sessions.find((s) => s.id === dragSessionId)
    setDragSessionId(null)
    if (!session || !canMoveSessionTo(groups, session, groupId)) return
    moveSession(dragSessionId, groupId)
  }, [dragSessionId, moveSession, sessions, groups])

  const handleSessionContextMenu = useCallback((session: SessionMeta, groupId?: string | null) => (e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    openContextMenu({
      x: e.clientX, y: e.clientY,
      selection: '', markdown: '',
      target: { type: 'session', id: session.id, groupId, provider: session.provider, model: session.model, isPinned: session.is_pinned },
    })
  }, [openContextMenu])

  const handleGroupContextMenu = useCallback((e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const el = (e.target as HTMLElement).closest('[data-group-id]') as HTMLElement
    const groupId = el?.dataset?.groupId
    if (groupId) {
      openContextMenu({
        x: e.clientX, y: e.clientY,
        selection: '', markdown: '',
        target: { type: 'group', id: groupId },
      })
    }
  }, [openContextMenu])

  return (
    <div style={{ width }} className="h-full bg-sidebar border-r border-surface-border flex flex-col flex-shrink-0 select-none">
      {/* New Session button — fixed height so it stays flush with the chat
          column's header row. */}
      <div className="px-2.5 h-14 flex items-center">
        <button
          onClick={() => { createSession(); showChat() }}
          className="w-full px-3 py-2 flex items-center justify-center gap-2 rounded-md bg-accent hover:bg-accent-hover text-accent-ink text-[13px] font-semibold transition-colors shadow-sm"
        >
          <span className="text-base leading-none">+</span>
          <span>{t('sessionList.newSession')}</span>
        </button>
      </div>

      {/* Navigation: Skills / MCP */}
      <div className="border-b border-surface-border">
        <button
          onClick={() => (mainView === 'skills' ? showChat() : showSkills())}
          className={`w-full px-3 py-2 flex items-center gap-2.5 text-left transition-colors ${
            mainView === 'skills'
              ? 'bg-accent-muted text-accent'
              : 'text-ink-soft hover:bg-sidebar-hover hover:text-ink'
          }`}
        >
          <span className="text-[13px] font-medium">{t('sessionList.skills')}</span>
        </button>
        <button
          onClick={() => (mainView === 'mcp' ? showChat() : showMcp())}
          className={`w-full px-3 py-2 flex items-center gap-2.5 text-left transition-colors ${
            mainView === 'mcp'
              ? 'bg-accent-muted text-accent'
              : 'text-ink-soft hover:bg-sidebar-hover hover:text-ink'
          }`}
        >
          <span className="text-[13px] font-medium">{t('sessionList.mcp')}</span>
        </button>
        <button
          onClick={() => (mainView === 'hooks' ? showChat() : showHooks())}
          className={`w-full px-3 py-2 flex items-center gap-2.5 text-left transition-colors ${
            mainView === 'hooks'
              ? 'bg-accent-muted text-accent'
              : 'text-ink-soft hover:bg-sidebar-hover hover:text-ink'
          }`}
        >
          <span className="text-[13px] font-medium">{t('sessionList.hooks')}</span>
        </button>
      </div>

      {/* Session list */}
      <div className="flex-1 overflow-y-auto">
        {/* Section header — always shown while sessions exist, regardless of
            search state, so "New Group" stays reachable. */}
        {sessions.length > 0 && (
          <div className="pt-6 px-3 pb-1.5 flex items-center justify-between">
            <span className="text-[10px] font-bold uppercase tracking-widest text-ink-faint">{t('sessionList.sessions')}</span>
            {!newGroupMode && (
              <button
                onClick={() => setNewGroupMode(true)}
                className="text-[10px] text-ink-faint hover:text-accent font-bold transition-colors"
                title={t('sessionList.newGroup')}
              >{t('sessionList.addGroup')}</button>
            )}
          </div>
        )}
        {newGroupMode && (
          <div className="px-3 pb-2">
            <input
              ref={newGroupRef}
              autoFocus
              placeholder={t('sessionList.groupNamePlaceholder')}
              onBlur={onNewGroupSubmit}
              onKeyDown={(e) => {
                if (e.key === 'Enter') onNewGroupSubmit()
                if (e.key === 'Escape') setNewGroupMode(false)
              }}
              className="w-full px-2 py-1 text-[12px] bg-surface-input rounded outline-none ring-1 ring-accent"
            />
          </div>
        )}

        {/* Search box — full-text over titles + message bodies. Sits under the
            "Sessions" header and above the (grouped or flat) list, so it stays
            visible while typing. */}
        <div className="px-3 pb-1">
          <div className="relative">
            <svg
              className="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-ink-faint/50 pointer-events-none"
              viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
            >
              <circle cx="11" cy="11" r="8" />
              <path d="m21 21-4.3-4.3" />
            </svg>
            <input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Escape') clearSearch() }}
              placeholder={t('sessionList.searchPlaceholder')}
              spellCheck={false}
              className="w-full pl-8 pr-7 py-1.5 text-[12px] bg-surface-input rounded-md text-ink placeholder:text-ink-faint/40 outline-none ring-1 ring-transparent focus:ring-accent transition-shadow"
            />
            {searchQuery && (
              <button
                onClick={clearSearch}
                className="absolute right-1.5 top-1/2 -translate-y-1/2 text-ink-faint/50 hover:text-ink w-4 h-4 flex items-center justify-center rounded text-[12px]"
                title={t('sessionList.clearSearch')}
              >&times;</button>
            )}
          </div>
        </div>

        {searchQuery.trim() ? (
          // ── Search mode: flat hit list (no grouping under a query) ──
          <div>
            <div className="pt-3 px-3 pb-1.5">
              <span className="text-[10px] font-semibold uppercase tracking-widest text-ink-faint/50">
                {searchResults === null ? t('sessionList.resultsPending') : t('sessionList.results', { count: searchResults.length })}
              </span>
            </div>
            {searchResults === null ? (
              <div className="px-3 py-2 text-[11px] text-ink-faint/40 italic">{t('sessionList.searching')}</div>
            ) : searchResults.length === 0 ? (
              <div className="px-3 py-2 text-[11px] text-ink-faint/40 italic">{t('sessionList.noMatches')}</div>
            ) : (
              searchResults.map((hit) => (
                <SearchResultItem
                  key={hit.meta.id}
                  hit={hit}
                  query={searchQuery}
                  active={hit.meta.id === activeSessionId}
                  onClick={() => { switchSession(hit.meta.id); showChat() }}
                  onDelete={() => deleteSession(hit.meta.id)}
                  onRename={(title) => renameSession(hit.meta.id, title)}
                  onContextMenu={handleSessionContextMenu(hit.meta)}
                />
              ))
            )}
          </div>
        ) : sessions.length === 0 ? (
          <div className="p-4 text-center">
            <div className="text-[13px] text-ink-faint/60 mb-3">{t('sessionList.noSessions')}</div>
            <button
              onClick={() => createSession()}
              className="text-[12px] text-accent hover:text-accent-hover font-medium"
            >{t('sessionList.createOne')}</button>
          </div>
        ) : (
          <>
            {/* Groups */}
            {/* Auto groups (working directory) */}
            {groups.filter((g) => g.is_auto).length > 0 && (
              <div className="pt-1 pb-0.5 px-3">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-ink-faint/50">{t('sessionList.projects')}</span>
              </div>
            )}
            {groups.filter((g) => g.is_auto).map((g) => (
              <div
                key={g.id}
                data-group-id={g.id}
                onDragOver={(e) => { if (canDropTo(g.id)) { e.preventDefault(); setDragOverGroupId(g.id) } }}
                onDragLeave={() => setDragOverGroupId(null)}
                onDrop={handleDropOnGroup(g.id)}
              >
                <CollapsibleGroup
                  group={g}
                  sessions={sessionsByGroup.get(g.id) || []}
                  activeSessionId={activeSessionId}
                  onSessionClick={(id) => { switchSession(id); showChat() }}
                  onSessionDelete={(id) => deleteSession(id)}
                  onSessionRename={(id, title) => renameSession(id, title)}
                  onSessionContextMenu={handleSessionContextMenu}
                  onRenameGroup={(id, name) => renameGroup(id, name)}
                  onDeleteGroup={(id) => {
                    if (window.confirm(t('sessionList.deleteProjectConfirm', { name: g.name }))) deleteGroup(id)
                  }}
                  onGroupContextMenu={handleGroupContextMenu}
                  onDrop={handleDropOnGroup(g.id)}
                  dragOver={dragOverGroupId === g.id}
                  editingSessionId={editingSessionId}
                  editingGroupId={editingGroupId}
                  onStartEditSession={startEditSession}
                  onCancelEditSession={cancelEditSession}
                  onStartEditGroup={startEditGroup}
                  onCancelEditGroup={cancelEditGroup}
                  displayName={projectLabels.get(g.id)}
                  onCreateSession={() => {
                    const cwd = sessionsByGroup.get(g.id)?.[0]?.cwd
                    if (cwd) { createSession(cwd); showChat() }
                  }}
                />
              </div>
            ))}

            {/* User-created groups */}
            {groups.filter((g) => !g.is_auto).length > 0 && (
              <div className="pt-4 pb-0.5 px-3">
                <span className="text-[10px] font-semibold uppercase tracking-widest text-ink-faint/50">{t('sessionList.groups')}</span>
              </div>
            )}
            {groups.filter((g) => !g.is_auto).map((g) => (
              <div
                key={g.id}
                data-group-id={g.id}
                onDragOver={(e) => { if (canDropTo(g.id)) { e.preventDefault(); setDragOverGroupId(g.id) } }}
                onDragLeave={() => setDragOverGroupId(null)}
                onDrop={handleDropOnGroup(g.id)}
              >
                <CollapsibleGroup
                  group={g}
                  sessions={sessionsByGroup.get(g.id) || []}
                  activeSessionId={activeSessionId}
                  onSessionClick={(id) => { switchSession(id); showChat() }}
                  onSessionDelete={(id) => deleteSession(id)}
                  onSessionRename={(id, title) => renameSession(id, title)}
                  onSessionContextMenu={handleSessionContextMenu}
                  onRenameGroup={(id, name) => renameGroup(id, name)}
                  onDeleteGroup={(id) => {
                    if (window.confirm(t('sessionList.deleteGroupConfirm', { name: g.name }))) deleteGroup(id)
                  }}
                  onGroupContextMenu={handleGroupContextMenu}
                  onDrop={handleDropOnGroup(g.id)}
                  dragOver={dragOverGroupId === g.id}
                  editingSessionId={editingSessionId}
                  editingGroupId={editingGroupId}
                  onStartEditSession={startEditSession}
                  onCancelEditSession={cancelEditSession}
                  onStartEditGroup={startEditGroup}
                  onCancelEditGroup={cancelEditGroup}
                />
              </div>
            ))}

            {/* Ungrouped sessions */}
            {rootSessions.length > 0 && (
              <div
                onDragOver={(e) => { e.preventDefault(); setDragOverGroupId(null) }}
                onDrop={handleDropOnRoot}
              >
                {rootSessions.map((s) => (
                  <div
                    key={s.id}
                    draggable
                    onDragStart={() => setDragSessionId(s.id)}
                  >
                    <SessionItem
                      session={s}
                      active={s.id === activeSessionId}
                      onClick={() => { switchSession(s.id); showChat() }}
                      onDelete={() => deleteSession(s.id)}
                      onRename={(title) => renameSession(s.id, title)}
                      onContextMenu={handleSessionContextMenu(s)}
                      editing={editingSessionId === s.id}
                      onStartEdit={() => startEditSession(s.id)}
                      onCancelEdit={cancelEditSession}
                      showCwd
                    />
                  </div>
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
