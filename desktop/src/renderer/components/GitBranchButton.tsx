import React, { useEffect, useRef, useState } from 'react'
import { useStore } from '../stores'
import BranchSwitchModal from './BranchSwitchModal'
import { useT } from '../useI18n'
import { tGlobal } from '../i18n'

// Compact branch pill in the working-dir bar. Hidden when the working directory
// isn't inside a git repo (or git is missing). Clicking opens an anchored
// dropdown of local branches; picking a dirty one prompts via BranchSwitchModal.
export default function GitBranchButton() {
  const t = useT()
  const git = useStore((s) => s.git)
  const workingDir = useStore((s) => s.workingDir)
  const refreshGitInfo = useStore((s) => s.refreshGitInfo)
  const setError = useStore((s) => s.setError)
  const isStreaming = useStore((s) => s.isStreaming)

  const [open, setOpen] = useState(false)
  const [branches, setBranches] = useState<string[]>([])
  const [current, setCurrent] = useState<string | null>(null)
  const [hasChanges, setHasChanges] = useState(false)
  const [pendingBranch, setPendingBranch] = useState<string | null>(null)
  const wrapRef = useRef<HTMLDivElement>(null)

  // Dismiss on outside click / Escape while the dropdown is open.
  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  // Auto-close while the agent is streaming — a branch switch mid-turn would
  // change the working tree under the running agent.
  useEffect(() => {
    if (isStreaming) setOpen(false)
  }, [isStreaming])

  // Early return AFTER all hooks so the hook count never varies between renders.
  if (!git?.inRepo) return null
  const label = git.currentBranch || t('git.noBranch')

  const toggle = async () => {
    if (isStreaming) return
    if (open) { setOpen(false); return }
    try {
      const list = await window.electronAPI.listGitBranches(workingDir)
      setBranches(list.branches)
      setCurrent(list.current)
      setHasChanges(list.hasChanges)
      setOpen(true)
    } catch (e: any) {
      setError(t('git.listFailed', { msg: e?.message || tGlobal('error.unknown') }))
    }
  }

  const pick = async (branch: string) => {
    if (isStreaming) return
    setOpen(false)
    if (branch === current) return
    if (hasChanges) {
      setPendingBranch(branch)
      return
    }
    await doCheckout(branch, 'direct')
  }

  const doCheckout = async (branch: string, strategy: 'direct' | 'stash' | 'commit' | 'discard') => {
    try {
      const res = await window.electronAPI.checkoutBranch(workingDir, branch, strategy)
      if (res.ok) {
        await refreshGitInfo()
      } else {
        setError(t('git.switchFailed', { msg: res.message || tGlobal('error.unknown') }))
      }
    } catch (e: any) {
      setError(t('git.switchFailed', { msg: e?.message || tGlobal('error.unknown') }))
    }
  }

  return (
    <div ref={wrapRef} className="relative flex-shrink-0">
      <button
        onClick={toggle}
        disabled={isStreaming}
        title={isStreaming ? t('git.disabledWorking') : label}
        className="text-xs px-2 py-0.5 rounded-md border border-surface-border text-ink-soft hover:text-accent hover:border-accent disabled:opacity-50 disabled:cursor-not-allowed disabled:hover:text-ink-soft disabled:hover:border-surface-border transition-colors font-mono flex items-center gap-1.5 max-w-[180px]"
      >
        ⎇ <span className="truncate">{label}</span>
      </button>

      {open && (
        <div
          className="absolute bottom-full mb-1 left-0 min-w-[180px] bg-surface-raised border border-surface-border rounded-lg shadow-lg overflow-hidden z-20"
          style={{ maxHeight: 240 }}
        >
          <div className="overflow-y-auto" style={{ maxHeight: 240 }}>
            {branches.length === 0 && (
              <div className="px-3 py-2 text-xs text-ink-faint">{t('git.noBranches')}</div>
            )}
            {branches.map((b) => (
              <button
                key={b}
                onClick={() => pick(b)}
                className={`w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs transition-colors ${
                  b === current ? 'text-accent bg-accent/10' : 'text-ink hover:bg-sidebar-hover'
                }`}
              >
                <span className="font-mono truncate flex-1">{b}</span>
                {b === current && <span className="text-[9px] text-accent">●</span>}
              </button>
            ))}
          </div>
        </div>
      )}

      {pendingBranch && (
        <BranchSwitchModal branch={pendingBranch} onClose={() => setPendingBranch(null)} />
      )}
    </div>
  )
}
