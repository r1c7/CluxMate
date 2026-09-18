import React, { useCallback, useEffect, useState } from 'react'
import { useStore } from '../stores'
import { useT } from '../useI18n'
import { tGlobal } from '../i18n'

// The usability pre-check for the worktree name. It is NOT the naming rule:
// normalisation (lowercase, collapse, trim, truncate) and slug suffixing live in
// Python (`cluxmate/core/worktree.py`) and stay authoritative — the CLI's
// `bad-name` / `branch-exists` answer is what decides. This only stops the one
// case the user cannot see coming: a name with no ASCII slug in it at all (a
// pure-CJK title slugs to "" and fails with `bad-name`), plus characters the
// rule would silently rewrite. Kept next to the field it guards rather than in
// shared/worktree-rules.ts, which holds the CLI contract, not dialog input.
const NAME_RE = /^[a-z0-9][a-z0-9-]*$/

// Cap on the uncommitted-file list rendered in the warning: enough to recognise
// the change, bounded so a huge dirty tree cannot push the fields off screen.
const MAX_LISTED_FILES = 30

interface GitState {
  current: string | null
  branches: string[]
  hasChanges: boolean
  files: string[]
}

// Modal for creating a session inside its own git worktree. Everything shown
// here is read from git; the only action that leaves the renderer is the create
// call (store.createWorktreeSession), and a failure renders the CLI's own
// message verbatim — that is the single exit for dirty / branch-exists /
// not-a-repo / bad-name.
export default function WorktreeDialog({ path }: { path: string }) {
  const t = useT()
  const closeDialog = useStore((s) => s.closeWorktreeDialog)
  const createWorktreeSession = useStore((s) => s.createWorktreeSession)

  const [name, setName] = useState('')
  const [branch, setBranch] = useState('')
  const [base, setBase] = useState('')
  const [git, setGit] = useState<GitState | null>(null)
  const [loading, setLoading] = useState(true)
  // `loadError` = could not read git at all; `opError` = a commit/stash attempt
  // failed; `error` = the create call failed. All three are rendered as-is.
  const [loadError, setLoadError] = useState<string | null>(null)
  const [opError, setOpError] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // Read branches + dirtiness for the project directory, then the changed-file
  // list when it is dirty. Re-run after a commit/stash so the warning clears
  // only once git confirms it did.
  const refresh = useCallback(async () => {
    setLoading(true)
    setLoadError(null)
    try {
      const list = await window.electronAPI.listGitBranches(path)
      let files: string[] = []
      if (list.hasChanges) {
        const status = await window.electronAPI.gitStatus({ cwd: path })
        if (status.ok) files = status.files || []
        else setLoadError(status.message || tGlobal('error.unknown'))
      }
      setGit({ current: list.current, branches: list.branches || [], hasChanges: list.hasChanges, files })
      // Default the base to the checked-out branch on first read; afterwards the
      // user's choice wins (a re-read must not silently move the base).
      setBase((prev) => prev || list.current || '')
    } catch (e: any) {
      setLoadError(e?.message || tGlobal('error.unknown'))
      setGit(null)
    } finally {
      setLoading(false)
    }
  }, [path])

  useEffect(() => { void refresh() }, [refresh])

  const runGitOp = async (op: 'commit' | 'stash') => {
    setBusy(true)
    setOpError(null)
    try {
      const res = op === 'commit'
        ? await window.electronAPI.gitCommitWip({ cwd: path })
        : await window.electronAPI.gitStash({ cwd: path })
      if (!res.ok) {
        setOpError(t('worktree.gitFailed', { msg: res.message || tGlobal('error.unknown') }))
        return
      }
      await refresh()
    } catch (e: any) {
      setOpError(t('worktree.gitFailed', { msg: e?.message || tGlobal('error.unknown') }))
    } finally {
      setBusy(false)
    }
  }

  const submit = async () => {
    setBusy(true)
    setError(null)
    try {
      const res = await createWorktreeSession({
        name: name.trim(),
        base: base.trim() || undefined,
        branch: branch.trim() || undefined,
      })
      // Success closes this dialog from the store (it unmounts), so only the
      // failure path still has a component to report into.
      if (!res.ok) setError(t('worktree.createFailed', { msg: res.message }))
    } catch (e: any) {
      setError(t('worktree.createFailed', { msg: e?.message || tGlobal('error.unknown') }))
    } finally {
      setBusy(false)
    }
  }

  const trimmedName = name.trim()
  const nameOk = NAME_RE.test(trimmedName)
  const dirty = !!git?.hasChanges
  const baseOk = !!base.trim()
  const canCreate = nameOk && baseOk && !dirty && !loading && !busy
  // A repo with no commits yet lists no branches; the CLI would answer
  // `base-missing` / `not-a-repo`, so say so before the click instead of after.
  const noBranches = !loading && !!git && !git.current && git.branches.length === 0
  const options = git
    ? Array.from(new Set([git.current, ...git.branches].filter((b): b is string => !!b)))
    : []

  return (
    <div className="fixed inset-0 bg-black/30 flex items-center justify-center z-50">
      <div className="bg-chat-agent rounded-xl w-[480px] max-h-[85vh] flex flex-col shadow-2xl border border-surface-border">
        <div className="flex items-center justify-between px-6 pt-5 pb-2">
          <h2 className="text-base font-semibold text-ink">{t('worktree.title')}</h2>
          <button onClick={closeDialog} className="text-ink-faint hover:text-ink text-xl">&times;</button>
        </div>
        <div className="px-6 pb-2 text-[11px] text-ink-faint/70 truncate" title={path}>{path}</div>

        {/* min-h-0 on the scroll body: a flex item's automatic minimum size is
            its content height, so without it a long dirty-file list would grow
            the panel past max-h and get clipped instead of scrolling. */}
        <div className="flex-1 min-h-0 overflow-y-auto px-6 py-3 space-y-4">
          {loadError && <Warning tone="error">{t('worktree.loadFailed', { msg: loadError })}</Warning>}

          {dirty && git && (
            <Warning tone="warn">
              <div className="font-medium">{t('worktree.dirtyTitle')}</div>
              <div className="mt-0.5 leading-snug">{t('worktree.dirtyBody')}</div>
              {git.files.length > 0 && (
                <>
                  <div className="mt-2 text-[11px] text-ink-soft">{t('worktree.dirtyFiles', { count: git.files.length })}</div>
                  <pre className="mt-1 max-h-28 overflow-y-auto text-[11px] font-mono text-ink-soft whitespace-pre-wrap break-words">
                    {git.files.slice(0, MAX_LISTED_FILES).join('\n')}
                    {git.files.length > MAX_LISTED_FILES ? '\n…' : ''}
                  </pre>
                </>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                <button
                  onClick={() => void runGitOp('commit')}
                  disabled={busy}
                  title={t('worktree.commitDesc')}
                  className="px-3 py-1.5 bg-surface-raised hover:bg-sidebar-hover text-ink text-xs rounded-lg border border-surface-border disabled:opacity-50"
                >{t('worktree.commit')}</button>
                <button
                  onClick={() => void runGitOp('stash')}
                  disabled={busy}
                  title={t('worktree.stashDesc')}
                  className="px-3 py-1.5 bg-surface-raised hover:bg-sidebar-hover text-ink text-xs rounded-lg border border-surface-border disabled:opacity-50"
                >{t('worktree.stash')}</button>
              </div>
            </Warning>
          )}

          {opError && <Warning tone="error">{opError}</Warning>}
          {error && <Warning tone="error">{error}</Warning>}

          <Field label={t('worktree.nameLabel')} help={t('worktree.nameHelp')}>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && canCreate) void submit() }}
              autoFocus
              spellCheck={false}
              placeholder={t('worktree.namePlaceholder')}
              className="w-full px-3 py-2 text-sm bg-surface-raised border border-surface-border rounded-lg text-ink outline-none focus:border-accent focus:ring-1 focus:ring-accent placeholder:text-ink-faint"
            />
            {trimmedName !== '' && !nameOk && (
              <div className="mt-1 text-[11px] text-red-600">{t('worktree.nameInvalid')}</div>
            )}
          </Field>

          <Field label={t('worktree.baseLabel')} help={t('worktree.baseHelp')}>
            <select
              value={base}
              onChange={(e) => setBase(e.target.value)}
              className="w-full px-3 py-2 text-sm bg-surface-raised border border-surface-border rounded-lg text-ink outline-none focus:border-accent focus:ring-1 focus:ring-accent"
            >
              {!baseOk && <option value="">{t('git.noBranch')}</option>}
              {options.map((b) => <option key={b} value={b}>{b}</option>)}
            </select>
            {noBranches && <div className="mt-1 text-[11px] text-amber-600">{t('worktree.noBranches')}</div>}
          </Field>

          <Field label={t('worktree.branchLabel')} help={t('worktree.branchHelp')}>
            <input
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && canCreate) void submit() }}
              spellCheck={false}
              placeholder={t('worktree.branchPlaceholder')}
              className="w-full px-3 py-2 text-sm bg-surface-raised border border-surface-border rounded-lg text-ink outline-none focus:border-accent focus:ring-1 focus:ring-accent placeholder:text-ink-faint"
            />
          </Field>
        </div>

        <div className="px-6 py-4 border-t border-surface-border flex items-center justify-end gap-2">
          {loading && <span className="mr-auto text-[11px] text-ink-faint">{t('common.loading')}</span>}
          <button
            onClick={closeDialog}
            disabled={busy}
            className="px-4 py-2 bg-surface-raised hover:bg-sidebar-hover text-ink text-sm rounded-lg border border-surface-border disabled:opacity-50"
          >{t('common.cancel')}</button>
          <button
            onClick={() => void submit()}
            disabled={!canCreate}
            className="px-4 py-2 bg-accent hover:bg-accent-hover disabled:bg-surface-raised text-accent-ink disabled:text-ink-faint text-sm rounded-lg disabled:opacity-50 disabled:cursor-not-allowed transition-colors font-medium"
          >{t('worktree.create')}</button>
        </div>
      </div>
    </div>
  )
}

function Field({ label, help, children }: {
  label: string
  help: string
  children: React.ReactNode
}) {
  return (
    <div>
      <label className="block text-xs font-medium text-ink mb-1">{label}</label>
      {children}
      <div className="mt-1 text-[11px] text-ink-faint leading-snug">{help}</div>
    </div>
  )
}

function Warning({ tone, children }: { tone: 'warn' | 'error'; children: React.ReactNode }) {
  const cls = tone === 'error'
    ? 'border-red-500/40 bg-red-500/5 text-red-600'
    : 'border-amber-500/40 bg-amber-500/5 text-amber-700'
  return (
    <div className={`rounded-lg border px-3 py-2 text-xs leading-relaxed ${cls}`}>{children}</div>
  )
}
