import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useStore } from '../stores'
import { useT } from '../useI18n'

function MenuItem({
  label, disabled, onClick,
}: {
  label: string; disabled?: boolean; onClick?: () => void
}) {
  return (
    <button
      disabled={disabled}
      onClick={onClick}
      className="w-full flex items-center gap-3 px-3 py-1.5 text-left text-xs text-ink hover:bg-accent/15 hover:text-accent disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-ink transition-colors"
    >
      <span className="flex-1">{label}</span>
    </button>
  )
}

function SubMenuItem({
  label, children,
}: {
  label: string
  children: React.ReactNode
}) {
  const [open, setOpen] = useState(false)
  const closeTimer = useRef<ReturnType<typeof setTimeout> | null>(null)

  // Hover-intent: the submenu opens immediately on hover, but closing is
  // deferred a beat. Without this, crossing the ~2px gap between the parent
  // item and the absolutely-positioned submenu fires `mouseleave` and the menu
  // flashes shut before the pointer can land on it.
  const cancelClose = () => {
    if (closeTimer.current) { clearTimeout(closeTimer.current); closeTimer.current = null }
  }
  const scheduleClose = () => {
    cancelClose()
    closeTimer.current = setTimeout(() => setOpen(false), 180)
  }
  useEffect(() => () => { if (closeTimer.current) clearTimeout(closeTimer.current) }, [])

  return (
    <div
      className="relative"
      onMouseEnter={() => { cancelClose(); setOpen(true) }}
      onMouseLeave={scheduleClose}
    >
      <button className="w-full flex items-center gap-3 px-3 py-1.5 text-left text-xs text-ink hover:bg-accent/15 hover:text-accent transition-colors">
        <span className="flex-1">{label}</span>
        <span className="text-[10px] text-ink-faint">▸</span>
      </button>
      {open && (
        <div
          className="absolute left-full top-0 ml-0.5 min-w-[160px] py-1 rounded-md border border-surface-border bg-surface-raised shadow-lg shadow-black/40 z-[61]"
          onMouseEnter={cancelClose}
          onMouseLeave={scheduleClose}
        >
          {children}
        </div>
      )}
    </div>
  )
}

function Separator() {
  return <div className="mx-2 my-1 border-t border-surface-border" />
}

export default function ContextMenu() {
  const t = useT()
  const menu = useStore((s) => s.contextMenu)
  const target = useStore((s) => s.contextMenuTarget)
  const close = useStore((s) => s.closeContextMenu)
  const groups = useStore((s) => s.groups)
  const deleteSession = useStore((s) => s.deleteSession)
  const deleteGroup = useStore((s) => s.deleteGroup)
  const moveSession = useStore((s) => s.moveSession)
  const moveSessionToProject = useStore((s) => s.moveSessionToProject)
  const pinSession = useStore((s) => s.pinSession)
  const startEditSession = useStore((s) => s.startEditSession)
  const startEditGroup = useStore((s) => s.startEditGroup)
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState({ x: 0, y: 0 })

  useLayoutEffect(() => {
    if (!menu) return
    const el = ref.current
    const w = el?.offsetWidth ?? 200
    const h = el?.offsetHeight ?? 120
    const pad = 8
    setPos({
      x: Math.min(menu.x, window.innerWidth - w - pad),
      y: Math.min(menu.y, window.innerHeight - h - pad),
    })
  }, [menu])

  useEffect(() => {
    if (!menu) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close()
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') close() }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    window.addEventListener('scroll', close, true)
    window.addEventListener('resize', close)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('resize', close)
    }
  }, [menu, close])

  if (!menu) return null

  const doDeleteSession = () => {
    if (target?.type === 'session') deleteSession(target.id)
    close()
  }

  const doDeleteGroup = () => {
    if (target?.type === 'group') {
      const p = groups.find((x) => x.id === target.id)
      const confirmKey = p?.is_auto ? 'sessionList.deleteProjectConfirm' : 'sessionList.deleteGroupConfirm'
      if (p && window.confirm(t(confirmKey, { name: p.name }))) {
        deleteGroup(target.id)
      }
    }
    close()
  }

  const doMoveSession = (groupId: string | null) => {
    if (target?.type === 'session') moveSession(target.id, groupId)
    close()
  }

  // Session context menu
  if (target?.type === 'session') {
    const currentGroup = groups.find((g) => g.id === target.groupId)
    const inUserGroup = !!currentGroup && !currentGroup.is_auto
    const userGroups = groups.filter((g) => !g.is_auto)
    return (
      <div
        ref={ref}
        style={{ left: pos.x, top: pos.y }}
        className="fixed z-[60] min-w-[188px] py-1 rounded-md border border-surface-border bg-surface-raised shadow-lg shadow-black/40"
        onContextMenu={(e) => e.preventDefault()}
      >
        {(target.provider || target.model) && (
          <div className="px-3 py-1.5 text-[11px] text-ink-faint/60 truncate select-text">
            {[target.provider, target.model].filter(Boolean).join(' / ')}
          </div>
        )}
        <Separator />
        {inUserGroup ? (
          <MenuItem label={t('contextMenu.moveBackToProject')} onClick={() => { moveSessionToProject(target.id); close() }} />
        ) : userGroups.length > 0 ? (
          <SubMenuItem label={t('contextMenu.moveToGroup')}>
            {userGroups.map((g) => (
              <MenuItem key={g.id} label={g.name} onClick={() => { doMoveSession(g.id); close() }} />
            ))}
          </SubMenuItem>
        ) : (
          <MenuItem label={t('contextMenu.moveToGroup')} disabled />
        )}
        <Separator />
        <MenuItem label={t('contextMenu.renameSession')} onClick={() => { startEditSession(target.id); close() }} />
        <Separator />
        {target.isPinned ? (
          <MenuItem label={t('contextMenu.unpin')} onClick={() => { pinSession(target.id, false); close() }} />
        ) : (
          <MenuItem label={t('contextMenu.pin')} onClick={() => { pinSession(target.id, true); close() }} />
        )}
        <Separator />
        <MenuItem label={t('contextMenu.deleteSession')} onClick={doDeleteSession} />
      </div>
    )
  }

  // Group context menu
  if (target?.type === 'group') {
    const group = groups.find((g) => g.id === target.id)
    return (
      <div
        ref={ref}
        style={{ left: pos.x, top: pos.y }}
        className="fixed z-[60] min-w-[188px] py-1 rounded-md border border-surface-border bg-surface-raised shadow-lg shadow-black/40"
        onContextMenu={(e) => e.preventDefault()}
      >
        {group && !group.is_auto && (
          <MenuItem label={t('contextMenu.renameGroup')} onClick={() => { startEditGroup(target.id); close() }} />
        )}
        <MenuItem
          label={t(group?.is_auto ? 'contextMenu.deleteProject' : 'contextMenu.deleteGroup')}
          onClick={doDeleteGroup}
        />
      </div>
    )
  }

  // Default: chat area copy actions (no target)
  const hasSelection = !!menu.selection
  return (
    <div
      ref={ref}
      style={{ left: pos.x, top: pos.y }}
      className="fixed z-[60] min-w-[176px] py-1 rounded-md border border-surface-border bg-surface-raised shadow-lg shadow-black/40"
      onContextMenu={(e) => e.preventDefault()}
    >
      <MenuItem label={t('contextMenu.copy')} disabled={!hasSelection} onClick={() => {
        if (menu.selection) window.electronAPI.writeClipboard(menu.selection)
        close()
      }} />
      <MenuItem label={t('contextMenu.copyMarkdown')} disabled={!hasSelection} onClick={() => {
        window.electronAPI.writeClipboard(menu.markdown || menu.selection)
        close()
      }} />
    </div>
  )
}
