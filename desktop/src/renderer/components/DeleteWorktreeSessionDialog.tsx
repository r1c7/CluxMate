import React, { useCallback, useEffect, useState } from 'react'
import { useStore } from '../stores'
import type { DeleteSessionPrompt, WorktreeRemovalInfo } from '../stores'
import { DialogButton, DialogShell, Warning } from './Dialog'
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
  // Lives in the store, not here: proving the row is gone and converging on that
  // proof are one step ("a fresh read that lacks the row is the only proof"), and
  // the convergence repeats deleteSession's own `set` for this id — session
  // states, the active pointer, the bridge dots and the groups reload — rather
  // than reaching into useStore.setState from a component.
  const adoptDeletedSession = useStore((s) => s.adoptDeletedSession)

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

  // Outcome 2: the session goes, the tree and its branch stay. On success
  // deleteSession has already converged the sidebar, so this only closes the
  // prompt; on a rejection the store's adoptDeletedSession re-reads and, only if
  // a fresh read proves the row is gone, finishes the convergence that action's
  // skipped `set` would have done.
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
      // rejection can equally mean "deleted, the list just could not be refreshed".
      // adoptDeletedSession adopts that proof (and converges the store with it);
      // an UNPROVEN absence stays a failure rather than a fabricated success.
      if (await adoptDeletedSession(prompt.sessionId)) {
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
    <DialogShell
      title={t('deleteWorktree.title')}
      closeLabel={t('common.close')}
      onClose={closeDeletePrompt}
      // Closing while busy is held: closing unmounts this dialog, and both
      // outcomes report their {ok:false} refusal through local state — removal
      // kills the bridge first and retries once after ~1.5s, so a late refusal
      // would land on a dead component and the user would never learn that
      // nothing was deleted.
      closeDisabled={busy}
      subtitle={info?.path}
      footerLeading={busy ? <span className="mr-auto text-[11px] text-ink-faint">{t('common.loading')}</span> : undefined}
      footer={(
        <>
          <DialogButton label={t('common.cancel')} onClick={closeDeletePrompt} disabled={busy} />
          <DialogButton
            label={t('deleteWorktree.deleteOnly')}
            onClick={() => void deleteOnly()}
            disabled={busy}
            title={keepHint}
          />
          {/* Disabled until the worktree facts are in (and when the session row is
              already gone, where there is nothing left to remove the tree by). */}
          <DialogButton
            tone="danger"
            label={t('deleteWorktree.removeBoth')}
            onClick={() => void removeBoth()}
            disabled={busy || !info}
            title={t('deleteWorktree.removeBothDesc')}
          />
        </>
      )}
    >
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
    </DialogShell>
  )
}
