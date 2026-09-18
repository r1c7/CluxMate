import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useStore } from '../stores'
import { canCreateWorktree, isWorktreeSession } from '../../shared/worktree-rules'
import { useT } from '../useI18n'

// How many uncommitted paths the removal confirm lists. The prompt is plain
// window.confirm text, so an unbounded list would be unreadable; the count line
// always states the real total.
const MAX_CONFIRM_FILES = 20

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
  // Which projects are git repositories (filled by the sidebar's probe). A
  // project with no repository has no tree to make, so its entry is withheld.
  const projectGit = useStore((s) => s.projectGit)
  const sessions = useStore((s) => s.sessions)
  const requestDeleteSession = useStore((s) => s.requestDeleteSession)
  const requestDeleteGroup = useStore((s) => s.requestDeleteGroup)
  const moveSession = useStore((s) => s.moveSession)
  const moveSessionToProject = useStore((s) => s.moveSessionToProject)
  const pinSession = useStore((s) => s.pinSession)
  const startEditSession = useStore((s) => s.startEditSession)
  const startEditGroup = useStore((s) => s.startEditGroup)
  const openWorktreeDialog = useStore((s) => s.openWorktreeDialog)
  const worktreeRemovalInfo = useStore((s) => s.worktreeRemovalInfo)
  const removeWorktreeSession = useStore((s) => s.removeWorktreeSession)
  const setError = useStore((s) => s.setError)
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

  // A plain delete for an ordinary session; for a worktree session the store
  // opens the three-outcome prompt instead (the tree would otherwise be stranded
  // with no entry left to remove it). The "remove worktree" item above stays the
  // explicit, no-prompt path to the removal itself.
  const doDeleteSession = () => {
    if (target?.type === 'session') requestDeleteSession(target.id)
    close()
  }

  // The group/project delete goes through the store, exactly like the sidebar's
  // two entries, so this menu cannot be the one way to delete a group WITHOUT
  // being asked about its worktrees: the store reads the preview first and only
  // falls back to the ordinary one-click confirm when the group holds no removable
  // tree (a group with none behaves as it always did — no extra click). Deciding
  // it here instead — a window.confirm followed by deleteGroup's default 'keep' —
  // is what used to delete every session in the group while silently stranding
  // their trees and branches on disk.
  const doDeleteGroup = () => {
    if (target?.type === 'group') void requestDeleteGroup(target.id)
    close()
  }

  const doMoveSession = (groupId: string | null) => {
    if (target?.type === 'session') moveSession(target.id, groupId)
    close()
  }

  // Remove a worktree session's tree (and its branch). The user is shown exactly
  // what the removal destroys — the tree's path, its branch, and the files inside
  // it that are not committed — so git is read BEFORE the prompt (and the menu
  // closed first, since that read is async). The removal itself is the store's;
  // its failure is surfaced as a toast rather than dropped.
  const doRemoveWorktree = async (sessionId: string) => {
    close()
    const info = await worktreeRemovalInfo(sessionId)
    if (!info) return
    const files = info.error
      ? t('contextMenu.removeWorktreeUnknown', { msg: info.error })
      : info.files.length === 0
        ? t('contextMenu.removeWorktreeClean')
        : t('contextMenu.removeWorktreeDirty', {
            count: info.files.length,
            files: info.files.slice(0, MAX_CONFIRM_FILES).join('\n')
              + (info.files.length > MAX_CONFIRM_FILES ? '\n…' : ''),
          })
    const ok = window.confirm(t('contextMenu.removeWorktreeConfirm', {
      name: info.name,
      path: info.path,
      branch: info.branch || t('git.noBranch'),
      files,
    }))
    if (!ok) return
    const res = await removeWorktreeSession(sessionId)
    if (!res.ok) {
      // A deliberate refusal is not a failure. `not-a-worktree` (the session no
      // longer runs in the tree it names) and `worktree-in-use` (another session
      // is still living in it) both mean NOTHING was deleted and the user has a
      // way forward, so they must not be announced as a removal that broke; only
      // the real execution errors (`git-failed`, `python-missing`, …) keep the
      // failure wording. The body stays the main process's own message.
      const refused = res.error === 'not-a-worktree' || res.error === 'worktree-in-use'
      setError(t(refused ? 'error.removeWorktreeBlocked' : 'error.removeWorktreeFailed', { msg: res.message }))
    }
  }

  // Session context menu
  if (target?.type === 'session') {
    const currentGroup = groups.find((g) => g.id === target.groupId)
    const inUserGroup = !!currentGroup && !currentGroup.is_auto
    const userGroups = groups.filter((g) => !g.is_auto)
    const session = sessions.find((s) => s.id === target.id)
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
        {/* Only a session that lives in a worktree has a tree to remove; a plain
            delete stays a delete (it must never touch a worktree). */}
        {isWorktreeSession(session) && (
          <MenuItem label={t('contextMenu.removeWorktree')} onClick={() => { void doRemoveWorktree(target.id) }} />
        )}
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
        {/* A project (auto group) is the only kind of group with a repository to
            build a worktree from — and the probe has to have CONFIRMED one: an
            unanswered path keeps the entry (canCreateWorktree is fail-open). */}
        {!!group?.is_auto && canCreateWorktree(projectGit[group.path || '']) && (
          <>
            <MenuItem
              label={t('contextMenu.newWorktreeSession')}
              onClick={() => { openWorktreeDialog(target.id); close() }}
            />
            <Separator />
          </>
        )}
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
