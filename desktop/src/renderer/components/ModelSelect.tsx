import React, { useEffect, useRef, useState } from 'react'
import { useStore } from '../stores'
import type { ModelEntry } from '../../shared/types'
import { reasoningOptionsFor, reasoningValuesFor, defaultReasoningValue, DEFAULT_EFFORT } from '../../shared/reasoning'
import { useT } from '../useI18n'

type Pane = 'root' | 'model' | 'effort'

// The composer's model seat, mirroring DeepSeek Harness web's ModelSelect: a
// trigger showing `model · effort` and a two-row root menu (模型 / 推理等级)
// that drills into the model list or the reasoning-level list. Selecting a model
// resets the effort to that model's default; both apply to the NEXT message
// (the Python side switches provider/effort per chat/send).
export default function ModelSelect() {
  const t = useT()
  const models = useStore((s) => s.models)
  const activeModelId = useStore((s) => s.activeModelId)
  const activeReasoningEffort = useStore((s) => s.activeReasoningEffort)
  const selectModel = useStore((s) => s.selectModel)
  const isStreaming = useStore((s) => s.isStreaming)

  const [open, setOpen] = useState(false)
  const [pane, setPane] = useState<Pane>('root')
  const wrapRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false)
        setPane('root')
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      // Escape backs out of a drilled pane first, then closes.
      if (pane !== 'root') setPane('root')
      else setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open, pane])

  // Auto-close while the agent is streaming — a switch would only half-apply.
  useEffect(() => {
    if (isStreaming) { setOpen(false); setPane('root') }
  }, [isStreaming])

  const current = models.find((m) => m.id === activeModelId)
  const efforts = reasoningOptionsFor(current)
  const effortLabel = activeReasoningEffort || ''
  const modelName = current?.model_name || current?.provider || t('modelSelect.noModel')
  const triggerLabel = effortLabel ? `${modelName} · ${effortLabel}` : modelName

  const toggle = () => {
    if (isStreaming) return
    if (open) { setOpen(false); setPane('root') }
    else { setPane('root'); setOpen(true) }
  }

  const close = () => { setOpen(false); setPane('root') }

  const pickModel = (m: ModelEntry) => {
    if (isStreaming) return
    // Keep the current value when it's still valid for the new model (the
    // "default" sentinel is always valid); otherwise fall back to that model's
    // default. The value is an independent runtime choice.
    const levels = reasoningValuesFor(m)
    const effort = activeReasoningEffort === DEFAULT_EFFORT
      || (activeReasoningEffort && levels.includes(activeReasoningEffort))
      ? activeReasoningEffort
      : defaultReasoningValue(m)
    void selectModel(m.id, effort)
    close()
  }

  const pickEffort = (effort: string) => {
    if (isStreaming) return
    void selectModel(activeModelId, effort)
    close()
  }

  return (
    <div ref={wrapRef} className="relative flex-shrink-0">
      <button
        type="button"
        disabled={isStreaming}
        onClick={toggle}
        title={isStreaming ? t('modelSelect.disabledWorking') : t('modelSelect.title')}
        className="text-xs px-2 py-0.5 rounded-md border border-surface-border text-ink-soft hover:text-ink hover:border-ink-faint transition-colors flex items-center gap-1.5 disabled:opacity-50 disabled:cursor-not-allowed"
      >
        <span className="truncate max-w-[440px]">{triggerLabel}</span>
        <svg className="w-3 h-3 shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && (
        <div className="absolute bottom-full mb-1 left-0 min-w-[240px] max-w-[380px] py-1 rounded-md border border-surface-border bg-surface-raised shadow-lg shadow-black/40 z-20">
          {pane === 'root' && (
            <>
              <button
                type="button"
                onClick={() => setPane('model')}
                className="w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs transition-colors hover:bg-sidebar-hover"
              >
                <span className="text-ink-faint shrink-0">{t('modelSelect.model')}</span>
                <span className="text-ink ml-auto truncate max-w-[150px]">{modelName}</span>
                <svg className="w-3 h-3 text-ink-faint shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="9 6 15 12 9 18" />
                </svg>
              </button>
              {efforts.length > 0 && (
                <button
                  type="button"
                  onClick={() => setPane('effort')}
                  className="w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs transition-colors hover:bg-sidebar-hover"
                >
                  <span className="text-ink-faint shrink-0">{t('modelSelect.reasoning')}</span>
                  <span className="text-ink ml-auto truncate max-w-[150px]">{effortLabel || '—'}</span>
                  <svg className="w-3 h-3 text-ink-faint shrink-0" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <polyline points="9 6 15 12 9 18" />
                  </svg>
                </button>
              )}
            </>
          )}

          {pane === 'model' && (
            <div className="max-h-64 overflow-y-auto">
              {models.length === 0 && (
                <div className="px-3 py-2 text-xs text-ink-faint">{t('modelSelect.noneConfigured')}</div>
              )}
              {models.map((m) => {
                const selected = m.id === activeModelId
                return (
                  <button
                    key={m.id}
                    type="button"
                    onClick={() => pickModel(m)}
                    className={`w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs transition-colors ${
                      selected ? 'text-accent bg-accent/10' : 'text-ink hover:bg-sidebar-hover'
                    }`}
                  >
                    <span className="flex-1 min-w-0 truncate">
                      {m.provider} <span className="text-ink-faint">/ {m.model_name || t('modelSelect.noModelName')}</span>
                    </span>
                    {selected && <span className="text-accent shrink-0">✓</span>}
                  </button>
                )
              })}
            </div>
          )}

          {pane === 'effort' && (
            <div className="max-h-64 overflow-y-auto">
              {efforts.map((v) => {
                const selected = v === activeReasoningEffort
                return (
                  <button
                    key={v}
                    type="button"
                    onClick={() => pickEffort(v)}
                    className={`w-full text-left px-3 py-1.5 flex items-center gap-2 text-xs transition-colors ${
                      selected ? 'text-accent bg-accent/10' : 'text-ink hover:bg-sidebar-hover'
                    }`}
                  >
                    <span className="flex-1">{v}</span>
                    {selected && <span className="text-accent shrink-0">✓</span>}
                  </button>
                )
              })}
            </div>
          )}
        </div>
      )}
    </div>
  )
}
