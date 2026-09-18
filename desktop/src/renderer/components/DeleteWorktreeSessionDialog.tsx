import React, { useCallback, useEffect, useState } from 'react'
import { useStore } from '../stores'
import type { DeleteSessionPrompt, WorktreeRemovalInfo } from '../stores'
import { useT } from '../useI18n'
import { tGlobal } from '../i18n'

// Cap on the uncommitted-file list rendered in the warning. The same bound
// WorktreeDialog uses: enough to recognise the change, bounded so a huge dirty
// tree cannot push the choices off screen.
const MAX_LISTED_FILES = 30

// The prompt a delete entry point opens when the session lives in a git worktree.
// Deleting such a session is the one delete that can strand something with no UI
// left to reach it — the tree directory and its branch — so instead of the plain,
// immediate delete there are three outcomes:
//
//   1. remove the worktree AND delete the session (store.removeWorktreeSession,
//      which kills the bridge, removes the tree and the branch, deletes the row)
//   2. delete the session only, leaving the tree and its branch on disk
//   3. cancel — nothing happens
//
// WHICH session this is about comes from the store (`prompt`), because the row is
// what the user is destroying; the live worktree facts, `busy` and the refusal
// text are local state here — the same division of labour WorktreeDialog uses.
export default function DeleteWorktreeSessionDialog({ prompt }: { prompt: DeleteSessionPrompt }) {
  const t = useT()
  const closeDeletePrompt = useStore((s) => s.closeDeletePrompt)
  const deleteSession = useStore((s) => s.deleteSession)
  const worktreeRemovalInfo = useStore((s) => s.worktreeRemovalInfo)
  const removeWorktreeSession = useStore((s) => s.removeWorktreeSession)

  const [info, setInfo] = useState<WorktreeRemovalInfo | null>(null)
  const [loading, setLoading] = useState(true)
  // `message` is the already-translated warning shown above the buttons; `blocked`
  // only decides its tone. A deliberate refusal (`not-a-worktree`,
  // `worktree-in-use`) is NOT a failure — nothing was deleted and exit 2 is still
  // available — so it is rendered as a warning, while an execution error stays an
  // error. Either way the dialog stays OPEN: that is the whole point of offering
  // the second exit.
  const [message, setMessage] = useState<string | null>(null)
  const [blocked, setBlocked] = useState(false)
  const [busy, setBusy] = useState(false)

  // Read what the removal would destroy — the same store call, and so the same
  // facts, as the "Remove Worktree…" entry. `info.error` means git could not be
  // read, and `info === null` means the session row is already gone; neither may
  // be rendered as "clean".
  const load = useCallback(async () => {
    setLoading(true)
    try {
      setInfo(await worktreeRemovalInfo(prompt.sessionId))
    } finally {
      setLoading(false)
    }
  }, [prompt.sessionId, worktreeRemovalInfo])

  useEffect(() => { void load() }, [load])

  // Outcome 1: the tree, the branch and the session all go. A refusal deletes
  // nothing and keeps the dialog open so the user can fall back to outcome 2.
  const removeBoth = async () => {
    setBusy(true)
    setMessage(null)
    setBlocked(false)
    try {
      const res = await removeWorktreeSession(prompt.sessionId)
      if (res.ok) {
        closeDeletePrompt()
        return
      }
      const msg = res.message || tGlobal('error.unknown')
      const refused = res.error === 'not-a-worktree' || res.error === 'worktree-in-use'
      setBlocked(refused)
      setMessage(t(refused ? 'error.removeWorktreeBlocked' : 'error.removeWorktreeFailed', { msg }))
    } catch (e: any) {
      setMessage(t('error.removeWorktreeFailed', { msg: e?.message || tGlobal('error.unknown') }))
    } finally {
      setBusy(false)
    }
  }

  // Outcome 2: the session goes, the tree and its branch stay. deleteSession
  // converges the sidebar itself; this only closes the prompt afterwards (it must
  // not also touch sessions/activeSessionId, which would fight that convergence).
  const deleteOnly = async () => {
    setBusy(true)
    setMessage(null)
    setBlocked(false)
    try {
      await deleteSession(prompt.sessionId)
      closeDeletePrompt()
    } catch (e: any) {
      // Only a row that is STILL there is a failure: `deleteSession` awaits its
      // groups reload after the main process has already deleted the row, so a
      // rejection can equally mean "deleted, the list just could not be refreshed"
      // (see sessionIsGone). Closing on that case reports the success it was; see
      // the same guard removeWorktreeSession puts around that reload.
      if (await sessionIsGone(prompt.sessionId)) {
        closeDeletePrompt()
        return
      }
      setMessage(t('deleteWorktree.deleteFailed', { msg: e?.message || tGlobal('error.unknown') }))
    } finally {
      setBusy(false)
    }
  }

  const branch = prompt.branch || t('git.noBranch')
  const dirty = !!info && !info.error && info.files.length > 0
  const clean = !!info && !info.error && info.files.length === 0
  // A blank name is the identity requestDeleteSession stores when it could not
  // read the row (the list refresh failed). Naming a worktree that was never read
  // — `""`, `(no branch)`, a bare `cluxmate worktree remove` — says nothing true,
  // so both the body and the keep-hint (plain and as the button's tooltip) have an
  // unnamed variant. The missing-row warning and the three exits are unaffected.
  const hasName = prompt.name.trim().length > 0
  const keepHint = hasName
    ? t('deleteWorktree.keepHint', { name: prompt.name })
    : t('deleteWorktree.keepHintNoName')

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-chat-agent rounded-xl w-[520px] max-h-[85vh] flex flex-col shadow-2xl border border-surface-border">
        <div className="flex items-center justify-between px-6 pt-5 pb-2">
          <h2 className="text-base font-semibold text-ink">{t('deleteWorktree.title')}</h2>
          {/* Disabled while busy like the three buttons below: closing unmounts this
              dialog, and both outcomes report their {ok:false} refusal through local
              state — removal kills the bridge first and retries once after ~1.5s, so
              a late refusal would land on a dead component and the user would never
              learn that nothing was deleted. */}
          <button
            onClick={closeDeletePrompt}
            disabled={busy}
            className="text-ink-faint hover:text-ink text-xl disabled:opacity-50"
          >&times;</button>
        </div>
        {info?.path && (
          <div className="px-6 pb-2 text-[11px] text-ink-faint/70 truncate" title={info.path}>{info.path}</div>
        )}

        {/* min-h-0 on the scroll body: a flex item's automatic minimum size is its
            content height, so without it a long dirty-file list would grow the
            panel past max-h and be clipped instead of scrolling. */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-3 space-y-4">
          <p className="text-sm text-ink-soft leading-relaxed">
            {/* Same empty-title fallback the sidebar card uses: a row with no
                title is rendered as "New Session", never as "". */}
            {hasName
              ? t('deleteWorktree.body', {
                title: prompt.title || t('sessionList.newSession'),
                name: prompt.name,
                branch,
              })
              : t('deleteWorktree.bodyNoName', { title: prompt.title || t('sessionList.newSession') })}
          </p>

          {loading && <div className="text-[11px] text-ink-faint">{t('common.loading')}</div>}

          {/* git could not be read: "clean" and "could not tell" must not look
              alike on a prompt that is about to delete a directory. */}
          {!loading && !!info?.error && (
            <Warning tone="error">{t('contextMenu.removeWorktreeUnknown', { msg: info.error })}</Warning>
          )}

          {/* The session row is gone (deleted from another window), so there is
              nothing left to remove the tree by — say so rather than show an
              empty, falsely reassuring listing. */}
          {!loading && !info && <Warning tone="warn">{t('deleteWorktree.missing')}</Warning>}

          {dirty && info && (
            <Warning tone="warn">
              <div className="font-medium">{t('deleteWorktree.dirtyTitle')}</div>
              <div className="mt-0.5 leading-snug">{t('deleteWorktree.dirtyBody')}</div>
              <div className="mt-2 text-[11px] text-ink-soft">{t('worktree.dirtyFiles', { count: info.files.length })}</div>
              <pre className="mt-1 max-h-28 overflow-y-auto text-[11px] font-mono text-ink-soft whitespace-pre-wrap break-words">
                {info.files.slice(0, MAX_LISTED_FILES).join('\n')}
                {info.files.length > MAX_LISTED_FILES ? '\n…' : ''}
              </pre>
            </Warning>
          )}

          {clean && (
            <div className="text-[11px] text-ink-faint">{t('contextMenu.removeWorktreeClean')}</div>
          )}

          {message && <Warning tone={blocked ? 'warn' : 'error'}>{message}</Warning>}

          {/* The second exit needs its consequence stated BEFORE the click: after
              the session is gone the UI has no entry to the tree at all, and the
              CLI is the only way left. */}
          <div className="text-[11px] text-ink-faint leading-snug">
            {keepHint}
          </div>
        </div>

        <div className="px-6 py-4 border-t border-surface-border flex flex-wrap items-center justify-end gap-2">
          {/* Removing a tree kills the session's bridge first and may retry once
              after a beat (a live process locks the directory on Windows), so the
              wait is not instant — dimmed buttons alone would read as a dead
              click. */}
          {busy && <span className="mr-auto text-[11px] text-ink-faint">{t('common.loading')}</span>}
          <button
            onClick={closeDeletePrompt}
            disabled={busy}
            className="px-4 py-2 bg-surface-raised hover:bg-sidebar-hover text-ink text-sm rounded-lg border border-surface-border disabled:opacity-50"
          >{t('common.cancel')}</button>
          <button
            onClick={() => void deleteOnly()}
            disabled={busy}
            title={keepHint}
            className="px-4 py-2 bg-surface-raised hover:bg-sidebar-hover text-ink text-sm rounded-lg border border-surface-border disabled:opacity-50"
          >{t('deleteWorktree.deleteOnly')}</button>
          {/* Disabled until the worktree facts are in (and when the session row is
              already gone, where there is nothing left to remove the tree by). */}
          <button
            onClick={() => void removeBoth()}
            disabled={busy || !info}
            title={t('deleteWorktree.removeBothDesc')}
            className="px-4 py-2 border border-red-500/40 bg-red-500/5 hover:bg-red-500/10 text-red-600 text-sm rounded-lg font-medium disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >{t('deleteWorktree.removeBoth')}</button>
        </div>
      </div>
    </div>
  )
}

// Did the session row actually go? A rejected `deleteSession` does NOT prove it did
// not: the action awaits listGroups() AFTER the main process has already deleted the
// row, and GROUP_LIST deliberately does not swallow its errors, so a failed reload
// rejects with the deletion done — the action's own `set` never runs, leaving a ghost
// row in the sidebar. Reporting that as a failure is a lie, and the ghost row keeps a
// "remove the worktree" entry the main process can only refuse as `not-a-worktree`:
// exactly the stranded tree this dialog exists to prevent. So the row's fate is
// re-read rather than assumed. SESSION_LIST is the call the sidebar itself refreshes
// with, and adopting its answer is also what clears that ghost row; the rows the
// store already holds are the fallback when even the re-read fails. Same reasoning as
// the guard removeWorktreeSession puts around its own groups reload.
async function sessionIsGone(sessionId: string): Promise<boolean> {
  try {
    useStore.setState({ sessions: await window.electronAPI.listSessions() })
  } catch { /* keep the rows we hold — they are the only evidence left */ }
  return !useStore.getState().sessions.some((s) => s.id === sessionId)
}

function Warning({ tone, children }: { tone: 'warn' | 'error'; children: React.ReactNode }) {
  const cls = tone === 'error'
    ? 'border-red-500/40 bg-red-500/5 text-red-600'
    : 'border-amber-500/40 bg-amber-500/5 text-amber-700'
  return (
    <div className={`rounded-lg border px-3 py-2 text-xs leading-relaxed ${cls}`}>{children}</div>
  )
}
