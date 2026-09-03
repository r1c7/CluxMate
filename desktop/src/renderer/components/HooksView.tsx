import React, { useEffect, useRef, useState } from 'react'
import { useStore } from '../stores'
import type { HookEntry, HooksScope } from '../../shared/types'
import { useT } from '../useI18n'

// Static reference for the help page. Event names are identifiers (English in
// both locales); the ✓/✗ columns read from this table.
const EVENTS: { name: string; block: boolean; inject: boolean; whenKey: string }[] = [
  { name: 'UserPromptSubmit', block: true, inject: true, whenKey: 'hooks.evtUserPrompt' },
  { name: 'PreToolUse', block: true, inject: true, whenKey: 'hooks.evtPreTool' },
  { name: 'PostToolUse', block: false, inject: true, whenKey: 'hooks.evtPostTool' },
  { name: 'Stop', block: true, inject: true, whenKey: 'hooks.evtStop' },
  { name: 'SessionStart', block: true, inject: true, whenKey: 'hooks.evtSessionStart' },
  { name: 'SessionEnd', block: false, inject: false, whenKey: 'hooks.evtSessionEnd' },
  { name: 'SubagentStop', block: true, inject: true, whenKey: 'hooks.evtSubagentStop' },
  { name: 'PreCompact', block: true, inject: true, whenKey: 'hooks.evtPreCompact' },
  { name: 'Notification', block: false, inject: false, whenKey: 'hooks.evtNotification' },
]

// Copyable example config (language-neutral — it's JSON).
const SAMPLE_CONFIG = `{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "bash",
        "hooks": [
          {"type": "command", "command": "python .cluxmate/hooks/audit.py", "timeout": 30}
        ]
      }
    ],
    "PostToolUse": [
      {"hooks": [{"type": "command", "command": "node .cluxmate/hooks/notify.js"}]}
    ],
    "Stop": [
      {"hooks": [{"type": "command", "command": "python .cluxmate/hooks/check.py"}]}
    ]
  }
}`

// Read-only view listing the project's active lifecycle hooks. Config lives in
// settings.json (global + project, merged by the Python side) — this view only
// surfaces it and links out to the file. "Help" opens a separate full-page
// reference rather than squeezing it alongside the list.
export default function HooksView() {
  const t = useT()
  const hooks = useStore((s) => s.hooks)
  const loading = useStore((s) => s.hooksLoading)
  const showHooks = useStore((s) => s.showHooks)
  const reloadHooks = useStore((s) => s.reloadHooks)
  const notifyHooks = useStore((s) => s.notifyHooks)
  const setError = useStore((s) => s.setError)
  const activeSessionId = useStore((s) => s.activeSessionId)
  const [view, setView] = useState<'list' | 'help'>('list')
  const [copied, setCopied] = useState(false)
  const [openMenu, setOpenMenu] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)

  // Re-fetch when the active session (hence project cwd) changes.
  useEffect(() => {
    showHooks()
  }, [activeSessionId])

  // Close the open-settings dropdown on an outside click.
  useEffect(() => {
    if (!openMenu) return
    const onDown = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setOpenMenu(false)
      }
    }
    window.addEventListener('mousedown', onDown)
    return () => window.removeEventListener('mousedown', onDown)
  }, [openMenu])

  const openSettings = async (scope: HooksScope) => {
    setOpenMenu(false)
    const sid = activeSessionId
    if (!sid) return
    try {
      // The main process resolves the path (global vs project), auto-creates the
      // file with an empty {"hooks":{}} skeleton if missing, then opens it.
      await window.electronAPI.openHooksSettings(sid, scope)
    } catch (e: any) {
      setError(t('error.openHooksFailed', { msg: e?.message }))
    }
  }

  const copySample = async () => {
    try {
      await window.electronAPI.writeClipboard(SAMPLE_CONFIG)
      setCopied(true)
      setTimeout(() => setCopied(false), 1500)
    } catch {
      setCopied(false)
    }
  }

  // "Help" is its own full page — swap out the list entirely.
  if (view === 'help') {
    return <HelpPage onBack={() => setView('list')} />
  }

  return (
    <div className="flex-1 flex flex-col min-w-0">
      {/* Header */}
      <div className="h-9 border-b border-surface-border flex items-center gap-2 px-4 flex-shrink-0">
        <span className="text-xs font-semibold text-ink-soft">{t('hooks.title')}</span>
        <span className="text-[10px] text-ink-faint">{hooks.length}</span>
        <div className="ml-auto flex items-center gap-2">
          <button
            onClick={() => reloadHooks()}
            disabled={loading}
            className="text-xs px-2.5 py-1 rounded-md border border-surface-border text-ink-soft hover:text-ink hover:bg-sidebar-hover transition-colors disabled:opacity-50"
            title={t('hooks.restartNote')}
          >
            {loading ? t('hooks.loading') : t('hooks.reload')}
          </button>

          <button
            onClick={() => notifyHooks(t('hooks.notifyMessage'))}
            className="text-xs px-2.5 py-1 rounded-md border border-surface-border text-ink-soft hover:text-ink hover:bg-sidebar-hover transition-colors"
            title={t('hooks.notifyHint')}
          >
            {t('hooks.notifyBtn')}
          </button>

          <button
            onClick={() => setView('help')}
            className="text-xs px-2.5 py-1 rounded-md border border-surface-border text-ink-soft hover:text-ink hover:bg-sidebar-hover transition-colors"
          >
            {t('hooks.help')}
          </button>

          {/* Open-settings dropdown: choose global vs project scope. */}
          <div ref={menuRef} className="relative">
            <button
              onClick={() => setOpenMenu((v) => !v)}
              className="text-xs px-2.5 py-1 rounded-md border border-surface-border text-ink-soft hover:text-ink hover:bg-sidebar-hover transition-colors flex items-center gap-1"
            >
              {t('hooks.openSettings')}
              <span className="text-[9px] text-ink-faint">▾</span>
            </button>
            {openMenu && (
              <div className="absolute right-0 top-full mt-1 z-20 w-56 rounded-md border border-surface-border bg-surface-raised shadow-md py-1">
                <button
                  onClick={() => openSettings('project')}
                  className="w-full text-left px-3 py-2 text-xs text-ink hover:bg-sidebar-hover transition-colors"
                >
                  {t('hooks.openProject')}
                </button>
                <button
                  onClick={() => openSettings('global')}
                  className="w-full text-left px-3 py-2 text-xs text-ink hover:bg-sidebar-hover transition-colors"
                >
                  {t('hooks.openGlobal')}
                </button>
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-y-auto">
        {loading && hooks.length === 0 ? (
          <p className="text-xs text-ink-faint px-4 py-3">{t('hooks.loading')}</p>
        ) : hooks.length === 0 ? (
          <div className="px-4 py-4 max-w-2xl">
            <p className="text-xs text-ink-soft leading-relaxed">{t('hooks.none')}</p>
            <div className="mt-3 rounded-md border border-surface-border bg-surface-raised/30 overflow-hidden">
              <div className="px-3 py-1.5 border-b border-surface-border flex items-center gap-2">
                <span className="text-[10px] uppercase tracking-wide text-ink-faint/70">{t('hooks.sampleTitle')}</span>
                <button
                  onClick={copySample}
                  className="ml-auto text-[11px] text-accent hover:text-accent-hover transition-colors"
                >
                  {copied ? t('hooks.copied') : t('hooks.copySample')}
                </button>
              </div>
              <pre className="px-3 py-2 text-[11px] font-mono text-ink-soft/90 overflow-x-auto select-text">{SAMPLE_CONFIG}</pre>
            </div>
            <p className="mt-2 text-[11px] text-ink-faint">{t('hooks.restartNote')}</p>
          </div>
        ) : (
          <div className="divide-y divide-surface-border/50">
            {hooks.map((h, i) => (
              <HookRow key={`${h.event}-${h.command}-${i}`} hook={h} />
            ))}
            <p className="px-4 py-2 text-[11px] text-ink-faint">{t('hooks.restartNote')}</p>
          </div>
        )}
      </div>
    </div>
  )
}

function HookRow({ hook }: { hook: HookEntry }) {
  const t = useT()
  return (
    <div className="px-4 py-2.5 hover:bg-sidebar-hover/50 transition-colors">
      <div className="flex items-center gap-2">
        <span className="text-xs font-mono text-accent">{hook.event}</span>
        <span className="text-[10px] text-ink-faint">
          {t('hooks.matcher')}:{' '}
          <span className="text-ink-soft">{hook.matcher ?? t('hooks.matcherAll')}</span>
        </span>
        <span className="text-[10px] text-ink-faint ml-auto flex-shrink-0">
          {t('hooks.timeout', { sec: Math.round(hook.timeout) })}
        </span>
      </div>
      <div className="mt-1 text-[11px] font-mono text-ink-soft/90 break-all select-text">{hook.command}</div>
    </div>
  )
}

// Full-page help reference. Rendered in place of the hooks list (its own page,
// with its own header + back button), so the long content gets the whole area
// and scrolls naturally.
function HelpPage({ onBack }: { onBack: () => void }) {
  const t = useT()
  return (
    <div className="flex-1 flex flex-col min-w-0">
      <div className="h-9 border-b border-surface-border flex items-center gap-2 px-4 flex-shrink-0">
        <button
          onClick={onBack}
          className="text-xs text-accent hover:text-accent-hover transition-colors flex items-center gap-1"
        >
          <span aria-hidden>←</span>
          {t('hooks.back')}
        </button>
        <span className="text-xs font-semibold text-ink-soft">{t('hooks.help')}</span>
      </div>

      <div className="flex-1 overflow-y-auto px-4 py-4 max-w-2xl select-text">
        <p className="text-xs text-ink-soft leading-relaxed">{t('hooks.helpIntro')}</p>

        <div className="mt-4">
          <h4 className="text-[10px] uppercase tracking-wide text-ink-faint/70 mb-1">{t('hooks.helpLocations')}</h4>
          <p className="text-[11px] font-mono text-ink-soft/90">{t('hooks.helpLocationsBody')}</p>
        </div>

        <div className="mt-4">
          <h4 className="text-[10px] uppercase tracking-wide text-ink-faint/70 mb-1">{t('hooks.helpEvents')}</h4>
          <table className="text-[11px] text-ink-soft w-full max-w-md">
            <thead>
              <tr className="text-left text-ink-faint">
                <th className="font-medium py-0.5 pr-3">{t('hooks.helpEventsCol')}</th>
                <th className="font-medium py-0.5 pr-3">{t('hooks.helpBlock')}</th>
                <th className="font-medium py-0.5 pr-3">{t('hooks.helpInject')}</th>
                <th className="font-medium py-0.5">{t('hooks.helpWhen')}</th>
              </tr>
            </thead>
            <tbody>
              {EVENTS.map((e) => (
                <tr key={e.name}>
                  <td className="py-0.5 pr-3 font-mono text-accent">{e.name}</td>
                  <td className="py-0.5 pr-3">{e.block ? '✓' : '—'}</td>
                  <td className="py-0.5 pr-3">{e.inject ? '✓' : '—'}</td>
                  <td className="py-0.5 text-ink-faint">{t(e.whenKey)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="mt-4">
          <h4 className="text-[10px] uppercase tracking-wide text-ink-faint/70 mb-1">{t('hooks.helpOutput')}</h4>
          <pre className="text-[11px] font-mono text-ink-soft/90 bg-surface-raised/40 rounded p-2 overflow-x-auto">
{`{"decision":"block","reason":"..."}            → block (reason shown to the model)
{"continue":false,"reason":"..."}              → block
exit code 2                                      → block
{"hookSpecificOutput":{"additionalContext":"…"}} → allow + inject context
(no output / non-JSON / exit 0)                  → allow`}
          </pre>
        </div>

        <ul className="mt-4 text-[11px] text-ink-faint list-disc list-inside space-y-1">
          <li>{t('hooks.helpNoteRestart')}</li>
          <li>{t('hooks.helpNoteCreate')}</li>
          <li>{t('hooks.helpNoteTrust')}</li>
          <li>{t('hooks.helpNoteFail')}</li>
          <li>{t('hooks.helpNoteFirst')}</li>
          <li>{t('hooks.helpNoteStop')}</li>
        </ul>
      </div>
    </div>
  )
}
