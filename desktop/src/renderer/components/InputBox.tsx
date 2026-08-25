import React, { useState, useRef, useEffect, useMemo } from 'react'
import { useStore } from '../stores'
import GitBranchButton from './GitBranchButton'
import ModeSelector from './ModeSelector'
import ModelSelect from './ModelSelect'
import { useT } from '../useI18n'

// Textarea auto-grows with content up to this cap (px), then scrolls.
const MAX_INPUT_HEIGHT = 320

interface SlashItem {
  slug: string
  /** Skill description text (skills carry their own copy). */
  description?: string
  /** i18n key for builtin command descriptions. */
  descriptionKey?: string
  kind: 'builtin' | 'skill'
}

// Framework-native slash commands understood by the agent via its system prompt.
// Listed before skills in the autocomplete dropdown. Descriptions are i18n keys
// resolved through the active language at render time.
const BUILTIN_COMMANDS: SlashItem[] = [
  { slug: 'init', descriptionKey: 'input.cmd.init', kind: 'builtin' },
  { slug: 'memory', descriptionKey: 'input.cmd.memory', kind: 'builtin' },
  { slug: 'clear', descriptionKey: 'input.cmd.clear', kind: 'builtin' },
]

export default function InputBox() {
  const t = useT()
  const sessionStates = useStore((s) => s.sessionStates)
  const [text, setText] = useState('')
  const sendMessage = useStore((s) => s.sendMessage)
  const cancelChat = useStore((s) => s.cancelChat)
  const isStreaming = useStore((s) => s.isStreaming)
  const messages = useStore((s) => s.messages)
  const activeSessionId = useStore((s) => s.activeSessionId)
  const inputDraft = useStore((s) => s.inputDraft)
  const consumeInputDraft = useStore((s) => s.consumeInputDraft)
  const setDraft = useStore((s) => s.setDraft)
  const skills = useStore((s) => s.skills)
  const loadSkills = useStore((s) => s.loadSkills)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const prevSessionRef = useRef<string | null>(activeSessionId)
  const [selIdx, setSelIdx] = useState(0)  // selected dropdown index

  // Build the combined slash-item list: builtins first, then skills.
  const slashItems: SlashItem[] = useMemo(() => {
    const skillItems: SlashItem[] = skills
      .filter((s) => !s.disabled)
      .map((s) => {
        // Slug is the directory name containing SKILL.md, not the frontmatter name.
        const parts = s.path.replace(/\\/g, '/').split('/')
        const slug = parts[parts.length - 2] || s.name
        return { slug, description: s.description || s.name, kind: 'skill' as const }
      })
    return [...BUILTIN_COMMANDS, ...skillItems]
  }, [skills])

  // When the user types a slash, detect it and load skills if not yet loaded.
  const slashPrefix = useMemo(() => {
    const cursorIdx = text.indexOf('/')
    if (cursorIdx === -1) return null
    // Only show autocomplete when the slash is at the very start, or preceded by
    // whitespace — avoids matching mid-text slashes (URLs, paths).
    if (cursorIdx > 0 && !/\s/.test(text[cursorIdx - 1])) return null
    // After the slash must be a query fragment that doesn't contain whitespace.
    const rest = text.slice(cursorIdx)
    if (/\s/.test(rest)) return null
    return rest
  }, [text])

  // Filter items matching the current prefix.
  const matches = useMemo(() => {
    if (!slashPrefix) return []
    const q = slashPrefix.toLowerCase()
    return slashItems.filter((item) =>
      item.slug.toLowerCase().startsWith(q.slice(1)) ||
      `/${item.slug.toLowerCase()}`.startsWith(q)
    )
  }, [slashPrefix, slashItems])

  // Lazily load skills the first time a slash is typed.
  const skillsLoaded = useRef(false)
  useEffect(() => {
    if (text.startsWith('/') && !skillsLoaded.current && activeSessionId) {
      skillsLoaded.current = true
      loadSkills()
    }
  }, [text, activeSessionId, loadSkills])

  // Reset skillsLoaded when the session changes so we refetch for the new project.
  useEffect(() => {
    skillsLoaded.current = false
  }, [activeSessionId])

  // Reset selection when matches change.
  useEffect(() => {
    setSelIdx(0)
  }, [matches])

  useEffect(() => {
    if (!isStreaming && textareaRef.current) {
      textareaRef.current.focus()
    }
  }, [isStreaming])

  // When switching sessions, save the current draft to the old session and
  // restore the new session's draft (or empty if none).
  useEffect(() => {
    const prev = prevSessionRef.current
    if (prev === activeSessionId) return

    // Save current text into the session being left.
    if (prev && prev !== activeSessionId) {
      const ss = sessionStates.get(prev)
      if (ss && ss.draftText !== text) {
        setDraft(prev, text)
      }
    }

    // Restore draft from the session being switched to.
    const ss = activeSessionId ? sessionStates.get(activeSessionId) : undefined
    const restored = ss?.draftText || ''
    setText(restored)

    // Reset textarea height to fit restored content.
    requestAnimationFrame(() => {
      const el = textareaRef.current
      if (el) {
        el.style.height = 'auto'
        el.style.height = Math.min(el.scrollHeight, MAX_INPUT_HEIGHT) + 'px'
        el.focus()
      }
    })

    prevSessionRef.current = activeSessionId
  }, [activeSessionId, sessionStates, setDraft])

  // Undo refills the input box with the undone message's text via a one-shot
  // draft. Load it, resize, focus, then clear the draft so it applies once.
  useEffect(() => {
    if (inputDraft == null) return
    setText(inputDraft)
    consumeInputDraft()
    const el = textareaRef.current
    if (el) {
      el.focus()
      el.style.height = 'auto'
      el.style.height = Math.min(el.scrollHeight, MAX_INPUT_HEIGHT) + 'px'
    }
  }, [inputDraft, consumeInputDraft])

  const handleSubmit = () => {
    const trimmed = text.trim()
    if (!trimmed || isStreaming) return
    sendMessage(trimmed)
    setText('')
    // Clear the per-session draft so it doesn't reappear on re-entry.
    if (activeSessionId) setDraft(activeSessionId, '')
    // Reset the auto-grown height back to the default rows after clearing.
    const el = textareaRef.current
    if (el) el.style.height = ''
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    // Dropdown navigation
    if (matches.length > 0) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setSelIdx((prev) => Math.min(prev + 1, matches.length - 1))
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setSelIdx((prev) => Math.max(prev - 1, 0))
        return
      }
      if (e.key === 'Tab' || e.key === 'Enter') {
        e.preventDefault()
        const item = matches[selIdx]
        if (item) {
          // Replace the /... with the full slug and a trailing space, keeping
          // any text before the slash.
          const cursorIdx = text.indexOf('/')
          if (cursorIdx >= 0) {
            const before = text.slice(0, cursorIdx)
            const after = text.slice(cursorIdx + (slashPrefix?.length || 0))
            setText(`${before}/${item.slug} ${after}`.trimStart())
          }
        }
        return
      }
      if (e.key === 'Escape') {
        e.preventDefault()
        setText(text.replace(slashPrefix || '', ''))
      }
    }

    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSubmit()
    }
  }

  // Per-session token totals: sum input tokens (from each agent message's
  // persisted `cacheUsage.input_tokens`) and output tokens (from each agent
  // message's `timing.out_tokens`), plus the cumulative usage folded into every
  // subagent node. Subagents live in a flat `message.subagents` map (keyed by
  // agent_id), so grandchildren are just more entries in the same map. Both
  // survive session switches and reopening — message fields ride the display
  // transcript, and subagent nodes are reconstructed on reload from the child
  // JSONL — so this is a pure projection over `messages`, no extra IPC or
  // backend state. Only rendered once tokens were actually accumulated (0/0).
  const sessionTokens = useMemo(() => {
    let input = 0
    let output = 0
    for (const m of messages) {
      if (m.role !== 'agent') continue
      if (m.cacheUsage?.input_tokens) input += m.cacheUsage.input_tokens
      if (m.timing?.out_tokens) output += m.timing.out_tokens
      for (const n of Object.values(m.subagents || {})) {
        input += n.input_tokens || 0
        output += n.output_tokens || 0
      }
    }
    return { input, output }
  }, [messages])

  // Format the footer's model-generation readout: first-token latency and token
  // rate (both exclude tool approval/execution time — see Python AgentResult).
  // Taken from the last agent message's persisted `timing` (exact provider token
  // counts), which survives session switches and reopening.
  const timingLabel = useMemo(() => {
    let timing: { ttft_ms: number | null; gen_ms: number; out_tokens: number } | undefined
    for (let i = messages.length - 1; i >= 0; i--) {
      const m = messages[i]
      if (m.role === 'agent' && m.timing) { timing = m.timing; break }
    }
    if (!timing || timing.out_tokens <= 0) return null
    const parts: string[] = []
    if (timing.ttft_ms != null) {
      parts.push(t('input.timing.firstToken', { sec: (timing.ttft_ms / 1000).toFixed(1) }))
    }
    if (timing.gen_ms > 0) {
      const rate = timing.out_tokens / (timing.gen_ms / 1000)
      parts.push(t('input.timing.rate', { rate: rate >= 100 ? Math.round(rate) : rate.toFixed(1) }))
    }
    return parts.length ? parts.join(' · ') : null
  }, [messages, t])

  return (
    <div className="border-t border-surface-border p-3 bg-sidebar relative">
      {/* Slash command autocomplete dropdown */}
      {matches.length > 0 && (
        <div
          className="absolute left-3 right-3 bottom-full mb-1 bg-surface-raised border border-surface-border rounded-lg shadow-lg overflow-hidden z-20"
          style={{ maxHeight: 240 }}
        >
          <div className="overflow-y-auto" style={{ maxHeight: 240 }}>
            {matches.map((item, i) => (
              <button
                key={`${item.kind}:${item.slug}`}
                type="button"
                className={`w-full text-left px-3 py-2 flex items-center gap-2.5 transition-colors ${
                  i === selIdx
                    ? 'bg-accent/15 text-accent'
                    : 'text-ink hover:bg-sidebar-hover'
                }`}
                onMouseDown={(e) => {
                  e.preventDefault()
                  const cursorIdx = text.indexOf('/')
                  if (cursorIdx >= 0) {
                    const before = text.slice(0, cursorIdx)
                    const after = text.slice(cursorIdx + (slashPrefix?.length || 0))
                    setText(`${before}/${item.slug} ${after}`.trimStart())
                  }
                  textareaRef.current?.focus()
                }}
              >
                <span className="text-xs font-mono text-ink-soft min-w-[48px]">
                  /{item.slug}
                </span>
                <span className="text-xs text-ink-faint truncate flex-1">
                  {item.descriptionKey ? t(item.descriptionKey) : item.description}
                </span>
                {item.kind === 'builtin' && (
                  <span className="text-[9px] px-1 py-0.5 rounded bg-ink-faint/10 text-ink-faint ml-auto flex-shrink-0">
                    {t('input.builtin')}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Toolbar row above the composer: model / mode / git branch. Model lives
          here so its name is never truncated and its picker opens upward into the
          chat log (on-screen). */}
      <div className="flex items-center gap-2 mb-2">
        <ModelSelect />
        <ModeSelector />
        <GitBranchButton />
      </div>

      <div className="flex items-end gap-2">
        <textarea
          ref={textareaRef}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={activeSessionId ? t('input.placeholder') : t('input.noSessionPlaceholder')}
          disabled={!activeSessionId}
          rows={3}
          className="flex-1 bg-surface-raised text-ink rounded-lg px-3 py-2 text-sm leading-relaxed resize-none outline-none border border-surface-border focus:border-accent focus:ring-1 focus:ring-accent disabled:opacity-50 placeholder:text-ink-faint"
          style={{ minHeight: '72px', maxHeight: `${MAX_INPUT_HEIGHT}px` }}
          onInput={(e) => {
            const el = e.currentTarget
            el.style.height = 'auto'
            el.style.height = Math.min(el.scrollHeight, MAX_INPUT_HEIGHT) + 'px'
          }}
        />
        {isStreaming ? (
          <button
            onClick={cancelChat}
            className="px-4 py-2 bg-red-700/80 hover:bg-red-700 text-white text-sm rounded-lg transition-colors"
          >{t('input.stop')}</button>
        ) : (
          <button
            onClick={handleSubmit}
            disabled={!text.trim() || !activeSessionId}
            className="px-4 py-2 bg-accent hover:bg-accent-hover disabled:bg-surface-raised text-accent-ink text-sm rounded-lg disabled:opacity-50 disabled:text-ink-faint transition-colors"
          >{t('input.send')}</button>
        )}
      </div>
      <div className="flex items-center justify-between mt-1.5 min-h-[16px]">
        <div className="flex items-center gap-2.5">
          <div className="text-xs text-ink-faint flex items-center gap-1.5" title={t('input.timing.title')}>
            {timingLabel && <span className="tabular-nums">{timingLabel}</span>}
          </div>
          {(sessionTokens.input > 0 || sessionTokens.output > 0) && (
            <div
              className="text-xs text-ink-faint flex items-center gap-1.5 tabular-nums"
              title={t('input.tokens.title')}
            >
              <span className="text-ink-faint/40">|</span>
              <span>{t('input.tokens.input', { count: sessionTokens.input.toLocaleString() })}</span>
              <span className="text-ink-faint/40">·</span>
              <span>{t('input.tokens.output', { count: sessionTokens.output.toLocaleString() })}</span>
            </div>
          )}
        </div>
        <div className="flex items-center gap-2.5">
          {isStreaming && (
            <div className="text-xs text-ink-faint flex items-center gap-1.5">
              <span className="inline-flex gap-0.5">
                <span className="w-1 h-1 rounded-full bg-accent animate-pulse" />
                <span className="w-1 h-1 rounded-full bg-accent animate-pulse" style={{ animationDelay: '0.2s' }} />
                <span className="w-1 h-1 rounded-full bg-accent animate-pulse" style={{ animationDelay: '0.4s' }} />
              </span>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
