import React, { useEffect, useRef, useState } from 'react'
import { useStore } from '../stores'
import type { PermissionMode } from '../../shared/types'
import { useT } from '../useI18n'

// Development-mode list. Order defines the
// list order in the popup. Each entry carries the button's label, dot/border/
// text classes, and a tooltip. Labels/tooltips are translated at render time via
// useT (the keys below resolve per language). yolo is styled as a warning — it
// auto-approves destructive commands (rm -rf, delete).
const MODE_ORDER: PermissionMode[] = ['plan', 'default', 'acceptEdits', 'yolo']
const MODE_UI: Record<PermissionMode, { labelKey: string; dot: string; cls: string; titleKey: string }> = {
  plan: {
    labelKey: 'mode.plan',
    dot: 'bg-blue-500',
    cls: 'border-blue-500/60 bg-blue-500/10 text-blue-600',
    titleKey: 'mode.planTitle',
  },
  default: {
    labelKey: 'mode.default',
    dot: 'bg-ink-faint',
    cls: 'border-surface-border text-ink-faint hover:text-ink hover:border-ink-faint',
    titleKey: 'mode.defaultTitle',
  },
  acceptEdits: {
    labelKey: 'mode.acceptEdits',
    dot: 'bg-accent',
    cls: 'border-accent/60 bg-accent/15 text-accent',
    titleKey: 'mode.acceptEditsTitle',
  },
  yolo: {
    labelKey: 'mode.yolo',
    dot: 'bg-red-500',
    cls: 'border-red-500/70 bg-red-500/15 text-red-600',
    titleKey: 'mode.yoloTitle',
  },
}

// Development-mode selector. Clicking the button opens an upward popup listing
// the four modes (mirrors the git-branch button's dropdown); picking one sets it.
export default function ModeSelector() {
  const t = useT()
  const mode = useStore((s) => s.mode)
  const setMode = useStore((s) => s.setMode)
  const isStreaming = useStore((s) => s.isStreaming)

  const [open, setOpen] = useState(false)
  const wrapRef = useRef<HTMLDivElement>(null)

  // Dismiss on outside click / Escape while the popup is open.
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

  // Auto-close while the agent is streaming — a mode switch mid-turn would only
  // half-apply (approval gate live, but toolset and the model's awareness only
  // change next turn), so keep mode changes to turn boundaries.
  useEffect(() => {
    if (isStreaming) setOpen(false)
  }, [isStreaming])

  const toggle = () => {
    if (isStreaming) return
    setOpen((o) => !o)
  }

  const pick = (m: PermissionMode) => {
    if (isStreaming) return
    setMode(m)
    setOpen(false)
  }

  return (
    <div ref={wrapRef} className="relative flex-shrink-0">
      <button
        type="button"
        disabled={isStreaming}
        onClick={toggle}
        title={isStreaming ? t('mode.disabledWhileWorking') : `${t(MODE_UI[mode].titleKey)} ${t('mode.takesEffectNext')}`}
        className={`text-xs px-2 py-0.5 rounded-md border transition-colors flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed ${MODE_UI[mode].cls}`}
      >
        <span className={`w-1.5 h-1.5 rounded-full ${MODE_UI[mode].dot}`} />
        {t(MODE_UI[mode].labelKey)}
      </button>

      {open && (
        <div className="absolute bottom-full mb-1 left-0 min-w-[170px] py-1 rounded-md border border-surface-border bg-surface-raised shadow-lg shadow-black/40 z-20">
          {MODE_ORDER.map((m) => (
            <button
              key={m}
              type="button"
              onClick={() => pick(m)}
              className={`w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs transition-colors ${
                m === mode ? 'text-accent bg-accent/10' : 'text-ink hover:bg-sidebar-hover'
              }`}
            >
              <span className={`w-1.5 h-1.5 rounded-full ${MODE_UI[m].dot}`} />
              <span className="flex-1">{t(MODE_UI[m].labelKey)}</span>
              {m === mode && <span className="text-[9px] text-accent">●</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
