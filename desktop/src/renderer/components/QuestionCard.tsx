import React, { useEffect, useState } from 'react'
import { useStore } from '../stores'
import type { Question, QuestionAnswer } from '../../shared/types'
import { useT } from '../useI18n'

// When a batch has this many questions or fewer, the card expands to show the
// tallest question in full (no internal scroll). Larger batches cap + scroll so
// a big batch can't push the chat area off-screen.
const FULLY_EXPANDED_QUESTIONS = 3

// Bottom-docked card for a pending ask_user_question prompt. It replaces the
// composer at the bottom of the conversation column but is inset + raised, so it
// reads as a distinct card (bg-chat-agent, border, shadow) rather than a full-
// width strip — and it never blocks the message list, header, sidebar, or
// Skills/MCP views. It shows ONE question at a time with < > paging at the
// bottom-left, renders options as a vertical list (radio single / checkbox
// multi), and keeps Skip/Submit at the bottom-right. Submit is disabled until
// every question is answered; the top-right ✕ and Skip are equivalent.
export default function QuestionCard() {
  const t = useT()
  const pending = useStore((s) => s.pendingQuestion)
  const answer = useStore((s) => s.answerQuestion)

  const [idx, setIdx] = useState(0)
  const [selections, setSelections] = useState<Record<string, string[]>>({})
  const [customs, setCustoms] = useState<Record<string, string>>({})

  const batchId = pending?.call_id ?? ''
  // Reset local state whenever a new question batch arrives.
  useEffect(() => {
    setIdx(0)
    setSelections({})
    setCustoms({})
  }, [batchId])

  if (!pending || pending.questions.length === 0) return null

  const total = pending.questions.length
  const q = pending.questions[idx]

  // ── per-question helpers (parametrized so all questions can render at once) ──
  const isSelected = (question: Question, label: string) => (selections[question.id] || []).includes(label)
  const customActive = (question: Question) => (customs[question.id]?.trim() ?? '') !== ''
  // Single-select: when a custom answer is typed, the options de-emphasize
  // (custom wins). Multi-select keeps options + custom visible together.
  const dimOptions = (question: Question) => !question.multi_select && customActive(question)

  // ── batch-level state ────────────────────────────────────────────────────
  // A question counts as answered once an option is selected OR free text typed.
  const isAnswered = (question: Question) => {
    const selected = (selections[question.id]?.length ?? 0) > 0
    const custom = (customs[question.id]?.trim() ?? '') !== ''
    return selected || custom
  }
  // Submit is enabled only once EVERY question is answered. Skip remains the
  // escape hatch for "proceed with defaults".
  const allAnswered = pending.questions.every(isAnswered)

  // Advance to the next unanswered question (circular, skipping the current one).
  // Used by Enter, and by single-select auto-advance.
  const jumpToNextUnanswered = () => {
    const n = pending.questions.length
    for (let step = 1; step < n; step++) {
      const j = (idx + step) % n
      if (!isAnswered(pending.questions[j])) {
        setIdx(j)
        return
      }
    }
  }

  const selectOne = (question: Question, label: string) => {
    setSelections((prev) => ({ ...prev, [question.id]: [label] }))
    // Single-select: picking an option clears any typed custom answer, so the
    // two stay mutually exclusive (custom would otherwise win).
    setCustoms((prev) => ({ ...prev, [question.id]: '' }))
    // Multi-question batch: after a single-select answer, auto-advance to the
    // next unanswered question so the user can fly through the batch. The
    // current question is skipped, so a fresh selection isn't re-read as the
    // "next" target. Multi-select does NOT auto-advance (the user may still be
    // ticking more boxes).
    if (total > 1) {
      jumpToNextUnanswered()
    }
  }
  const toggleMulti = (question: Question, label: string) => {
    setSelections((prev) => {
      const cur = prev[question.id] || []
      return { ...prev, [question.id]: cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label] }
    })
  }
  const setCustom = (question: Question, value: string) => {
    setCustoms((prev) => ({ ...prev, [question.id]: value }))
    // Single-select: typing a custom answer clears the selected option — custom
    // and option are mutually exclusive, and custom takes precedence.
    // Multi-select keeps both (they may coexist).
    if (!question.multi_select && value.trim() !== '') {
      setSelections((prev) => ({ ...prev, [question.id]: [] }))
    }
  }

  const submit = (skip: boolean) => {
    const answers: QuestionAnswer[] = pending.questions.map((question) => {
      const selected = skip ? [] : selections[question.id] || []
      const custom = skip ? undefined : (customs[question.id] || '').trim() || undefined
      return { id: question.id, selected, ...(custom ? { custom } : {}) }
    })
    answer(pending.call_id, answers)
  }

  const onInputKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    // Enter submits when every question is answered, otherwise it jumps to the
    // next unanswered question. The isComposing guard prevents IME candidate-
    // confirmation Enter (Chinese/Japanese input) from firing either action.
    if (e.key !== 'Enter' || e.nativeEvent.isComposing) return
    e.preventDefault()
    if (allAnswered) {
      submit(false)
    } else {
      jumpToNextUnanswered()
    }
  }

  return (
    <div className="border-t border-surface-border bg-chat pl-14 pr-4 py-3 shrink-0">
      <div className="rounded-xl border border-surface-border bg-chat-agent shadow-md">
        {/* Header: question header (top-left) + close(=skip) (top-right) */}
        <div className="flex items-center gap-2 px-4 pt-3">
          <span className="text-sm font-semibold text-ink truncate">{q.header || t('question.fallbackHeader')}</span>
          <button
            onClick={() => submit(true)}
            title={t('question.skipTitle')}
            className="ml-auto text-ink-faint hover:text-ink text-lg leading-none w-6 h-6 shrink-0"
          >✕</button>
        </div>

        {/* Body: all questions stacked in one grid cell, so the card height is
            fixed to the TALLEST question (no jump while paging). Non-current
            questions stay mounted but are `invisible` (occupy layout, no paint),
            so the grid row always sizes to the max. Small batches expand fully;
            larger batches cap + scroll. */}
        <div className={`px-4 py-2 ${total > FULLY_EXPANDED_QUESTIONS ? 'max-h-[40vh] overflow-y-auto' : ''}`}>
          <div className="grid grid-cols-1">
            {pending.questions.map((question, i) => {
              const visible = i === idx
              const activeCustom = customActive(question)
              return (
                <div key={question.id} className={`row-start-1 col-start-1 ${visible ? '' : 'invisible'}`}>
                  <div className="text-sm text-ink mb-2.5">{question.question}</div>

                  {question.options && question.options.length > 0 && (
                    <div className={`space-y-1.5 mb-2 transition-opacity duration-200 ${dimOptions(question) ? 'opacity-40' : ''}`}>
                      {question.options.map((opt) => {
                        const active = isSelected(question, opt.label)
                        return (
                          <label
                            key={opt.label}
                            className={`flex items-center gap-2.5 px-3 py-2 rounded-lg border cursor-pointer transition-colors ${
                              active ? 'border-accent bg-accent/10' : 'border-surface-border hover:border-accent/50'
                            }`}
                          >
                            <input
                              type={question.multi_select ? 'checkbox' : 'radio'}
                              name={`q-${question.id}`}
                              checked={active}
                              onChange={() => (question.multi_select ? toggleMulti(question, opt.label) : selectOne(question, opt.label))}
                              className="accent-accent shrink-0"
                            />
                            <span className="flex items-center gap-2 min-w-0 flex-1">
                              <span className="text-sm text-ink whitespace-nowrap shrink-0">{opt.label}</span>
                              {opt.description && (
                                <span className="text-xs text-ink-faint truncate min-w-0">{opt.description}</span>
                              )}
                            </span>
                          </label>
                        )
                      })}
                    </div>
                  )}

                  {/* Custom answer line — present on every question so the user can
                      type their own answer instead of (or in addition to) an option. */}
                  <input
                    value={customs[question.id] || ''}
                    onChange={(e) => setCustom(question, e.target.value)}
                    onKeyDown={onInputKeyDown}
                    placeholder={question.options && question.options.length > 0 ? t('question.otherPlaceholder') : t('question.typePlaceholder')}
                    className={`w-full bg-surface border text-ink rounded-lg px-3 py-1.5 text-sm outline-none focus:border-accent focus:ring-1 focus:ring-accent placeholder:text-ink-faint ${
                      activeCustom ? 'border-accent' : 'border-surface-border'
                    }`}
                  />
                </div>
              )
            })}
          </div>
        </div>

        {/* Footer: pager (bottom-left) + actions (bottom-right) */}
        <div className="flex items-center justify-between gap-2 px-4 pb-3 pt-1">
          <div className="flex items-center gap-1">
            {total > 1 && (
              <>
                <button
                  onClick={() => setIdx((i) => Math.max(0, i - 1))}
                  disabled={idx === 0}
                  className="w-6 h-6 rounded border border-surface-border text-ink-soft hover:border-accent/50 disabled:opacity-40 disabled:cursor-default"
                >‹</button>
                <span className="text-xs text-ink-faint tabular-nums min-w-[40px] text-center">{idx + 1} / {total}</span>
                <button
                  onClick={() => setIdx((i) => Math.min(total - 1, i + 1))}
                  disabled={idx === total - 1}
                  className="w-6 h-6 rounded border border-surface-border text-ink-soft hover:border-accent/50 disabled:opacity-40 disabled:cursor-default"
                >›</button>
              </>
            )}
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => submit(true)}
              className="px-4 py-1.5 bg-transparent hover:bg-surface-border text-ink-soft text-sm rounded-lg font-medium transition-colors"
            >{t('question.skip')}</button>
            <button
              onClick={() => submit(false)}
              disabled={!allAnswered}
              className="px-4 py-1.5 bg-accent hover:bg-accent-hover disabled:bg-surface-raised disabled:text-ink-faint disabled:cursor-not-allowed text-accent-ink text-sm rounded-lg font-medium transition-colors"
            >{t('question.submit')}</button>
          </div>
        </div>
      </div>
    </div>
  )
}
