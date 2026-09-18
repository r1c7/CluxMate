import React, { useCallback, useEffect, useState } from 'react'
import { useStore } from '../stores'
import type { DeleteGroupPrompt } from '../stores'
import type { GroupDeletePreview } from '../../shared/types'
import { DialogButton, DialogShell, Warning } from './Dialog'
import { useT } from '../useI18n'
import { tGlobal } from '../i18n'

// Cap on the uncommitted-file list rendered per worktree. The same bound
// DeleteWorktreeSessionDialog and WorktreeDialog use: enough to recognise the
// change, bounded so a huge dirty tree cannot push the choices off screen.
const MAX_LISTED_FILES = 30

// The prompt a group/project delete opens when the sessions inside it run in git
// worktrees. The group delete is the BULK version of the per-session prompt: it
// removes every session in the group at once, so a plain confirm would strand
// each of their trees and branches on disk with no UI entry left to reach them —
// the exact harm the per-session prompt exists to prevent. Three outcomes:
//
//   1. remove those worktrees AND delete the sessions (store.deleteGroup(id,
//      'remove'): the main process validates EVERY tree first and refuses as a
//      whole — naming the occupied one — when a session from outside the group is
//      still living in it; nothing is deleted on that refusal)
//   2. delete the sessions only, leaving every tree and branch on disk
//      (store.deleteGroup(id, 'keep') — today's behaviour, verbatim)
//   3. cancel — nothing happens
//
// WHICH group this is about comes from the store (`prompt`); the live facts
// (`preview`), `busy` and the refusal text are local state here — the same
// division of labour DeleteWorktreeSessionDialog uses, and the reason the store
// holds only the identity.
export default function DeleteGroupDialog({ prompt }: { prompt: DeleteGroupPrompt }) {
  const t = useT()
  const closeDeleteGroupPrompt = useStore((s) => s.closeDeleteGroupPrompt)
  const groupDeletePreview = useStore((s) => s.groupDeletePreview)
  const deleteGroup = useStore((s) => s.deleteGroup)

  const [preview, setPreview] = useState<GroupDeletePreview | null>(null)
  const [loading, setLoading] = useState(true)
  // Why the preview could not be read. Kept apart from `message` because it
  // disables the removal exit: without the preview there is no honest way to say
  // what a 'remove' would take.
  const [loadError, setLoadError] = useState<string | null>(null)
  // `message` is the already-translated text shown above the buttons; `blocked`
  // only decides its tone. A deliberate refusal (`worktree-in-use`) is NOT a
  // failure — nothing was deleted and exit 2 is still available — so it renders
  // as a warning, while an execution error stays an error. Either way the dialog
  // stays OPEN: that is the whole point of offering the second exit.
  const [message, setMessage] = useState<string | null>(null)
  const [blocked, setBlocked] = useState(false)
  const [busy, setBusy] = useState(false)

  // Read what the removal would destroy. The facts are re-read here (rather than
  // handed over by requestDeleteGroup) so what the user confirms is what the main
  // process sees a moment later, and so a failed read is visible as itself.
  const load = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      setPreview(await groupDeletePreview(prompt.groupId))
    } catch (e: any) {
      setPreview(null)
      setLoadError(e?.message || tGlobal('error.unknown'))
    } finally {
      setLoading(false)
    }
  }, [prompt.groupId, groupDeletePreview])

  useEffect(() => { void load() }, [load])

  // Outcome 1: the trees, their branches and every session in the group all go.
  // A refusal deletes nothing and keeps the dialog open so the user can fall back
  // to outcome 2 — the message shown is the main process's own, verbatim.
  const removeAll = async () => {
    setBusy(true)
    setMessage(null)
    setBlocked(false)
    try {
      const res = await deleteGroup(prompt.groupId, 'remove')
      if (res.ok) {
        // A non-empty `failed` means some trees stayed on disk while their
        // sessions went: deleteGroup toasts that itself (this dialog is closing,
        // and the plain-confirm path has no dialog at all to show it in).
        closeDeleteGroupPrompt()
        return
      }
      const msg = res.message || tGlobal('error.unknown')
      const refused = res.error === 'worktree-in-use'
      setBlocked(refused)
      setMessage(t(refused ? 'error.groupWorktreesBlocked' : 'error.groupDeleteFailed', { msg }))
    } catch (e: any) {
      setMessage(t('error.groupDeleteFailed', { msg: e?.message || tGlobal('error.unknown') }))
    } finally {
      setBusy(false)
    }
  }

  // Outcome 2: the sessions go, every tree and branch stays. The store converges
  // the sidebar itself, so this only closes the prompt.
  const deleteOnly = async () => {
    setBusy(true)
    setMessage(null)
    setBlocked(false)
    try {
      await deleteGroup(prompt.groupId, 'keep')
      closeDeleteGroupPrompt()
    } catch (e: any) {
      setMessage(t('error.groupDeleteFailed', { msg: e?.message || tGlobal('error.unknown') }))
    } finally {
      setBusy(false)
    }
  }

  const trees = preview?.worktrees ?? []
  const stale = preview?.stale ?? []
  const blockedTrees = preview?.blocked ?? []
  // Which candidates the main process would refuse. The exit is DISABLED with the
  // reason shown (rather than left clickable and answered with a refusal): the
  // user asked to delete a group and should not have to translate a rejection.
  // The main process still re-validates — occupancy can change between this read
  // and the click — so a live refusal is rendered above as well.
  const occupied = new Set(blockedTrees.map((b) => b.sessionId))
  const cannotRemove = blockedTrees.length > 0

  // An auto group IS a project, and the confirm this prompt replaces worded
  // itself accordingly (SessionList's deleteProjectConfirm), so the noun travels
  // with the prompt's identity and every sentence below uses it.
  const noun = t(prompt.isAuto ? 'deleteGroup.nounProject' : 'deleteGroup.nounGroup')
  const name = prompt.name || t('deleteGroup.unnamed', { noun })
  // TWO sentences, each counting one thing: the sessions that go, and the
  // worktrees this delete would remove. One sentence could only say the worktree
  // count as a share of the SESSIONS ("{trees} of them run in a worktree"), which
  // is false as soon as two sessions share one tree, and the flat dictionary has
  // no plural machinery — hence a singular and a plural key per sentence. The
  // tree sentence is omitted entirely at zero trees (the `noneFound` warning says
  // why), and the count-bearing first sentence needs the preview: without it,
  // "all 0 sessions" would be a statement this dialog cannot back up.
  const body = !preview
    ? t('deleteGroup.bodyUnknown', { name, noun })
    : t(preview.sessions === 1 ? 'deleteGroup.bodySessionsOne' : 'deleteGroup.bodySessions', {
        name,
        noun,
        count: preview.sessions,
      }) + (trees.length === 0
        ? ''
        : ' ' + t(trees.length === 1 ? 'deleteGroup.bodyTreeOne' : 'deleteGroup.bodyTrees', { trees: trees.length }))

  return (
    <DialogShell
      title={t(prompt.isAuto ? 'deleteGroup.titleProject' : 'deleteGroup.title')}
      closeLabel={t('common.close')}
      onClose={closeDeleteGroupPrompt}
      // Closing while busy is held: closing unmounts this dialog, and both
      // outcomes report their {ok:false} refusal through local state — removal
      // kills bridges first and may retry once after ~1.5s per tree, so a late
      // refusal would land on a dead component and the user would never learn
      // that nothing was deleted.
      closeDisabled={busy}
      width="w-[560px]"
      footerLeading={busy ? <span className="mr-auto text-[11px] text-ink-faint">{t('common.loading')}</span> : undefined}
      footer={(
        <>
          <DialogButton label={t('common.cancel')} onClick={closeDeleteGroupPrompt} disabled={busy} />
          <DialogButton
            label={t('deleteGroup.deleteOnly')}
            onClick={() => void deleteOnly()}
            disabled={busy}
            title={t('deleteGroup.keepHint')}
          />
          {/* Disabled until the facts are in, and when a tree is occupied (the
              reason is shown above). The main process refuses the whole batch
              again anyway — this button being enabled is not the authority. */}
          <DialogButton
            tone="danger"
            label={t('deleteGroup.removeAll')}
            onClick={() => void removeAll()}
            disabled={busy || !preview || trees.length === 0 || cannotRemove}
            title={cannotRemove ? t('deleteGroup.blockedTitle') : t('deleteGroup.removeAllDesc')}
          />
        </>
      )}
    >
      <p className="text-sm text-ink-soft leading-relaxed">{body}</p>

      {loading && <div className="text-[11px] text-ink-faint">{t('common.loading')}</div>}

      {/* The preview could not be read, so nothing here can claim what would
          be removed. The removal exit is disabled; deleting the sessions and
          keeping the trees is still offered, because that is the outcome that
          cannot lose work. */}
      {!loading && loadError && (
        <Warning tone="error">{t('deleteGroup.previewFailed', { msg: loadError })}</Warning>
      )}

      {!loading && preview && trees.length === 0 && (
        <Warning tone="warn">{t('deleteGroup.noneFound')}</Warning>
      )}

      {!loading && trees.length > 0 && (
        <div className="space-y-2">
          <div className="text-xs font-medium text-ink">{t('deleteGroup.worktreesTitle', { count: trees.length })}</div>
          {trees.map((tree) => (
            <div key={tree.path} className="rounded-lg border border-surface-border px-3 py-2 space-y-1">
              <div className="flex flex-wrap items-baseline gap-x-2 text-xs text-ink">
                <span className="font-medium">{tree.name || tree.path}</span>
                <span className="text-ink-faint">{t('deleteGroup.branch', { branch: tree.branch || t('git.noBranch') })}</span>
                {occupied.has(tree.sessionId) && (
                  <span className="text-amber-700">{t('deleteGroup.inUse')}</span>
                )}
              </div>
              <div className="text-[11px] text-ink-faint/70 truncate" title={tree.path}>{tree.path}</div>
              {/* git could not be read: "clean" and "could not tell" must not
                  look alike on a prompt that is about to delete a directory. */}
              {tree.dirtyError ? (
                <div className="text-[11px] text-amber-700">
                  {t('contextMenu.removeWorktreeUnknown', { msg: tree.dirtyError })}
                </div>
              ) : tree.dirty.length === 0 ? (
                <div className="text-[11px] text-ink-faint">{t('contextMenu.removeWorktreeClean')}</div>
              ) : (
                <>
                  <div className="text-[11px] text-ink-soft">{t('worktree.dirtyFiles', { count: tree.dirty.length })}</div>
                  <pre className="max-h-24 overflow-y-auto text-[11px] font-mono text-ink-soft whitespace-pre-wrap break-words">
                    {tree.dirty.slice(0, MAX_LISTED_FILES).join('\n')}
                    {tree.dirty.length > MAX_LISTED_FILES ? '\n…' : ''}
                  </pre>
                </>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Who blocks what, by name: a refusal the user can act on. */}
      {blockedTrees.length > 0 && (
        <Warning tone="warn">
          <div className="font-medium">{t('deleteGroup.blockedTitle')}</div>
          {blockedTrees.map((b) => (
            <div key={b.sessionId} className="mt-1 leading-snug">
              {t('deleteGroup.blockedBody', {
                name: b.name,
                blockers: b.blockers.map((x) => x.title || x.id).join(', '),
              })}
            </div>
          ))}
        </Warning>
      )}

      {/* Stale rows are stated, not silently dropped: the group may hold a
          session that still records a tree it no longer runs in, and that tree
          is NOT touched (nor can it be — nothing here knows where it is). */}
      {stale.length > 0 && (
        <div className="text-[11px] text-ink-faint leading-snug">
          {t('deleteGroup.staleNote', { count: stale.length })}
        </div>
      )}

      {message && <Warning tone={blocked ? 'warn' : 'error'}>{message}</Warning>}

      {/* The second exit's consequence stated BEFORE the click: after the
          sessions are gone the UI has no entry to those trees at all, and the
          CLI is the only way left. */}
      {trees.length > 0 && (
        <div className="text-[11px] text-ink-faint leading-snug">{t('deleteGroup.keepHint')}</div>
      )}
    </DialogShell>
  )
}
