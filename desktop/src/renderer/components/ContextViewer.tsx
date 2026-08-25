import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useStore } from '../stores'
import type { TurnCompaction, TurnContext, TurnStep } from '../../shared/types'
import { useT } from '../useI18n'

// Convert the header's canonical (Anthropic-style) tool schema into the exact
// OpenAI Chat Completions form that actually went on the wire: the provider's
// _convert_tools wraps each tool as {type:"function", function:{name,
// description, parameters}} (renaming input_schema → parameters).
function openAiTools(tools: Record<string, unknown>[]): Record<string, unknown>[] {
  return tools.map((t) => {
    const inner = (t as any).function ?? {}
    const name = typeof t.name === 'string' ? t.name : inner.name
    const description = typeof t.description === 'string' ? t.description : inner.description
    const parameters = t.input_schema ?? inner.parameters ?? { type: 'object' }
    return { type: 'function', function: { name, description, parameters } }
  })
}

// Reconstruct the exact OpenAI Chat Completions request the model saw for a
// step (what the provider sent on the wire), so it can be copied and replayed
// against the API verbatim. This is the read-side inverse of the
// "Model-visible ⟺ logged" invariant, re-serialized into OpenAI wire form.
function rawRequest(t: TurnContext, s: TurnStep): string {
  const cfg = t.config as any
  const body: Record<string, unknown> = {
    model: cfg?.model,
    max_completion_tokens: cfg?.max_tokens,
    messages: [{ role: 'system', content: t.system ?? '' }, ...s.messages],
  }
  if (t.tools.length > 0) {
    body.tools = openAiTools(t.tools)
  }
  return JSON.stringify(body, null, 2)
}

function messageText(m: Record<string, unknown>): string {
  const content = m.content
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content
      .map((b: any) => (typeof b?.text === 'string' ? b.text : b?.type ? `<${b.type} block>` : ''))
      .join('')
  }
  return content == null ? '' : JSON.stringify(content)
}

interface ToolCallView { name: string; arguments: string }

function messageToolCalls(m: Record<string, unknown>): ToolCallView[] {
  const tcs = m.tool_calls
  if (!Array.isArray(tcs)) return []
  return tcs.map((tc: any) => {
    const fn = tc.function ?? {}
    const args = typeof fn.arguments === 'string'
      ? fn.arguments
      : JSON.stringify(fn.arguments ?? tc.input ?? {})
    return { name: fn.name ?? tc.name ?? '?', arguments: args }
  })
}

function prettyArgs(args: string): string {
  try {
    return JSON.stringify(JSON.parse(args), null, 2)
  } catch {
    return args
  }
}

// Long bubbles (text or tool args) collapse to a truncated preview by default.
const COLLAPSE_LEN = 100

function CollapsibleText({ text, className, maxLen = COLLAPSE_LEN }: { text: string; className: string; maxLen?: number }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  if (text.length <= maxLen) {
    return <pre className={className}>{text}</pre>
  }
  return (
    <div>
      <div className="flex items-center justify-between mt-0.5">
        <button
          onClick={() => setOpen((v) => !v)}
          className="text-[10px] text-accent hover:underline"
        >
          {open ? t('context.collapse') : t('context.showAll')}
        </button>
        <span className="text-[10px] text-ink-faint">{t('context.chars', { count: text.length })}</span>
      </div>
      <pre className={className}>
        {open ? text : text.slice(0, maxLen) + '…'}
      </pre>
    </div>
  )
}

// Per-message-type color coding: a colored left accent + dot + uppercase label,
// tinted just enough to tell user / assistant / tool call / tool result apart at
// a glance while staying within the warm-white palette.
interface RoleStyle {
  labelKey: string
  border: string   // border-l-{color} accent stripe
  dot: string      // status dot fill
  text: string     // inline `color` (theme-aware)
  bg: string       // inline `background-color` (theme-aware)
}

const ROLE_STYLE: Record<'user' | 'assistant' | 'toolCall' | 'toolResult' | 'compaction' | 'injection', RoleStyle> = {
  user:       { labelKey: 'context.role.user',        border: 'border-l-amber-400',  dot: 'bg-amber-500',  text: 'rgb(var(--role-user))',       bg: 'rgb(var(--role-user-bg))' },
  assistant:  { labelKey: 'context.role.assistant',   border: 'border-l-accent',     dot: 'bg-accent',     text: 'rgb(var(--accent))',          bg: 'rgb(var(--surface-raised) / 0.4)' },
  toolCall:   { labelKey: 'context.role.toolCall',    border: 'border-l-sky-400',    dot: 'bg-sky-500',    text: 'rgb(var(--role-toolcall))',   bg: 'rgb(var(--role-toolcall-bg))' },
  toolResult: { labelKey: 'context.role.toolResult',  border: 'border-l-emerald-400', dot: 'bg-emerald-500', text: 'rgb(var(--role-toolresult))', bg: 'rgb(var(--role-toolresult-bg))' },
  compaction: { labelKey: 'context.role.compaction',  border: 'border-l-violet-400', dot: 'bg-violet-500', text: 'rgb(var(--role-compaction))', bg: 'rgb(var(--role-compaction-bg))' },
  injection:  { labelKey: 'context.role.injection',   border: 'border-l-stone-400',  dot: 'bg-stone-500',  text: 'rgb(var(--role-injection))',  bg: 'rgb(var(--role-injection-bg))' },
}

function toolResultId(m: Record<string, unknown>): string {
  return typeof m.tool_call_id === 'string' ? m.tool_call_id : ''
}

// Tool schemas are Anthropic-style in the header ({name, description,
// input_schema}); accept OpenAI-style ({type:"function", function:{...}}) too.
function toolName(t: Record<string, unknown>): string {
  return typeof t.name === 'string' ? t.name : (t as any).function?.name ?? '?'
}

function toolDescription(t: Record<string, unknown>): string {
  if (typeof t.description === 'string') return t.description
  return (t as any).function?.description ?? ''
}

function toolSchema(t: Record<string, unknown>): string {
  const s = t.input_schema ?? (t as any).function?.parameters
  return s ? JSON.stringify(s, null, 2) : ''
}

function Bubble({ style, meta, children }: { style: RoleStyle; meta?: React.ReactNode; children: React.ReactNode }) {
  const t = useT()
  return (
    <div className={`rounded-md border border-surface-border border-l-2 ${style.border} px-2.5 py-1.5`} style={{ backgroundColor: style.bg }}>
      <div className="flex items-center gap-1.5 min-w-0">
        <span className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${style.dot}`} />
        <span className="text-[10px] font-semibold uppercase tracking-wider flex-shrink-0" style={{ color: style.text }}>{t(style.labelKey)}</span>
        {meta}
      </div>
      {children}
    </div>
  )
}

// A compaction summary bubble: shows the summary plus a "turn/step" badge, and
// can expand to reveal the messages that were compacted away (marked as not part
// of this turn's model context).
function CompactionBubble({ summaryText, meta, currentTurn }: { summaryText: string; meta: TurnCompaction; currentTurn: number }) {
  const t = useT()
  const [showShadowed, setShowShadowed] = useState(false)
  const stepLabel = meta.step != null ? t('context.step', { step: meta.step }) : t('context.stepUnknown')
  const where = meta.turn !== currentTurn ? t('context.turnStep', { turn: meta.turn, step: stepLabel }) : stepLabel
  return (
    <Bubble style={ROLE_STYLE.compaction} meta={<span className="text-[10px] font-mono text-ink-faint truncate min-w-0 ml-1">{where}</span>}>
      <CollapsibleText text={summaryText} className="text-[11px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft mt-1" />
      {meta.shadowed.length > 0 && (
        <div className="mt-1">
          <button onClick={() => setShowShadowed((v) => !v)} className="text-[10px] hover:underline" style={{ color: 'rgb(var(--role-compaction))' }}>
            {showShadowed ? t('context.hideCompacted') : t('context.showCompacted', { count: meta.shadowed.length, plural: meta.shadowed.length === 1 ? '' : 's' })}
          </button>
          {showShadowed && (
            <div className="mt-1.5 space-y-1 border-t border-surface-border pt-1.5">
              <p className="text-[10px] text-ink-faint italic">{t('context.compactedAway')}</p>
              {meta.shadowed.map((m, j) => {
                const role = (m.role as string) || '?'
                const text = messageText(m as Record<string, unknown>)
                const calls = messageToolCalls(m as Record<string, unknown>)
                return (
                  <div key={j} className="rounded border border-surface-border bg-surface px-2 py-1 opacity-75">
                    <span className="text-[9px] font-semibold uppercase tracking-wide text-ink-faint">{role}</span>
                    {text.trim() !== '' && (
                      <CollapsibleText text={text} className="text-[10px] leading-relaxed whitespace-pre-wrap break-words text-ink-faint mt-0.5" />
                    )}
                    {calls.length > 0 && (
                      <span className="text-[10px] font-mono text-ink-faint block mt-0.5">
                        {calls.map((c) => c.name).join(', ')}
                      </span>
                    )}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )}
    </Bubble>
  )
}

export default function ContextViewer() {
  const t = useT()
  const open = useStore((s) => s.contextOpen)
  const turnContexts = useStore((s) => s.turnContexts)
  const contextLoading = useStore((s) => s.contextLoading)
  const loadTurnContexts = useStore((s) => s.loadTurnContexts)
  const toggle = useStore((s) => s.toggleContext)
  const activeSessionId = useStore((s) => s.activeSessionId)
  const isStreaming = useStore((s) => s.isStreaming)
  const contextTarget = useStore((s) => s.contextTarget)
  const backToAgentTree = useStore((s) => s.backToAgentTree)

  const [selected, setSelected] = useState(0)
  const [selectedStep, setSelectedStep] = useState(0)
  const [showRaw, setShowRaw] = useState(false)
  const [systemOpen, setSystemOpen] = useState(false)
  const [toolsOpen, setToolsOpen] = useState(false)
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (open) loadTurnContexts(contextTarget?.sessionId ?? null)
  }, [open, activeSessionId])

  // Auto-refresh while open: when a turn starts or finishes (isStreaming toggles),
  // reload so the panel always shows the latest turn. On a fresh message the new
  // turn appears (its user message is already logged); on completion the full
  // turn context lands. The selected index then auto-jumps to the newest below.
  const prevStreaming = useRef(isStreaming)
  useEffect(() => {
    if (open && prevStreaming.current !== isStreaming) {
      loadTurnContexts(contextTarget?.sessionId ?? null)
    }
    prevStreaming.current = isStreaming
  }, [isStreaming, open, loadTurnContexts])

  useEffect(() => {
    setSelected(turnContexts.length > 0 ? turnContexts.length - 1 : 0)
  }, [turnContexts.length])

  // Switching turns resets to that turn's first step.
  useEffect(() => {
    setSelectedStep(0)
  }, [selected])

  const current: TurnContext | undefined = turnContexts[selected]
  const currentStep: TurnStep | undefined = current?.steps?.[selectedStep]
  const raw = useMemo(() => (current && currentStep ? rawRequest(current, currentStep) : ''), [current, currentStep])

  if (!open) return null

  const copyRaw = async () => {
    if (!raw) return
    try {
      await window.electronAPI.writeClipboard(raw)
      setCopied(true)
      setTimeout(() => setCopied(false), 1200)
    } catch { /* best effort */ }
  }

  return (
    <div className="h-full flex flex-col bg-surface">
      <div className="flex items-center gap-2 px-3 h-9 border-b border-surface-border flex-shrink-0">
        <button
          onClick={() => setShowRaw((v) => !v)}
          disabled={!currentStep}
          className={`text-[11px] px-2 py-0.5 rounded border transition-colors disabled:opacity-50 ${
            showRaw ? 'border-accent/40 text-accent bg-accent/10' : 'border-surface-border text-ink-soft hover:bg-surface-raised hover:text-ink'
          }`}
        >
          {t('context.raw')}
        </button>
        <div className="flex-1" />
        <button
          onClick={copyRaw}
          disabled={!currentStep}
          className="text-[11px] px-2 py-0.5 rounded border border-surface-border text-ink-soft hover:bg-surface-raised hover:text-ink disabled:opacity-50 transition-colors"
          title={t('context.copyRawTitle')}
        >
          {copied ? t('context.copied') : t('context.copyRaw')}
        </button>
        <button
          onClick={() => toggle(false)}
          className="text-ink-faint hover:text-ink text-base px-1 transition-colors"
          title={t('common.close')}
        >×</button>
      </div>

      <div className="flex-1 overflow-y-auto">
        {/* Whose context this is — the main agent, or a subagent opened from the
            Agent Tree. Makes a subagent's panel unmistakable from the parent's. */}
        {contextTarget && (
          <div className="flex items-center gap-2 px-2.5 pt-2 pb-1.5 border-b border-surface-border">
            <div className="min-w-0">
              <div className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">{t('context.subagent')}</div>
              <div className="text-[11px] font-mono text-ink-soft truncate mt-0.5" title={contextTarget.sessionId}>
                {contextTarget.label} · {contextTarget.sessionId}
              </div>
            </div>
            <button
              onClick={backToAgentTree}
              className="ml-auto flex-shrink-0 text-xs px-2.5 py-1.5 rounded-md bg-accent/15 text-accent font-semibold border border-accent/40 hover:bg-accent/25 hover:border-accent/60 transition-colors"
              title={t('context.agentTreeTitle')}
            >
              {t('context.agentTree')}
            </button>
          </div>
        )}

        {contextLoading && turnContexts.length === 0 ? (
          <p className="text-xs text-ink-faint px-3 py-3">{t('context.loading')}</p>
        ) : turnContexts.length === 0 ? (
          <p className="text-xs text-ink-faint px-3 py-3">
            {contextTarget ? t('context.noSubagent') : t('context.none')}
          </p>
        ) : (
          <>
            {/* Turn selector — labeled so it reads as "pick which historical
                turn's context to inspect", not a bare dropdown. */}
            <div className="px-2 pt-2 pb-1.5 border-b border-surface-border">
              <div className="flex items-baseline justify-between px-0.5">
                <span className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">{t('context.turnHistory')}</span>
                <span className="text-[10px] text-ink-faint">{t('context.turns', { count: turnContexts.length, plural: turnContexts.length === 1 ? '' : 's' })}</span>
              </div>
              <select
                value={selected}
                onChange={(e) => setSelected(Number(e.target.value))}
                className="w-full bg-surface-raised border border-surface-border rounded text-xs text-ink px-2 py-1.5 mt-1 focus:outline-none focus:border-accent"
              >
                {turnContexts.map((t2, i) => (
                  <option key={t2.turn} value={i}>
                    {t('context.turn', { turn: t2.turn })}
                  </option>
                ))}
              </select>
              <p className="text-[10px] text-ink-faint px-0.5 mt-1.5 leading-relaxed">
                {t('context.turnHint')}
              </p>
            </div>

            {current && currentStep && (
              <div className="px-2 py-2 space-y-2">
                {/* Step selector — only when the turn ran tools (multiple LLM calls). */}
                {current.steps.length > 1 && (
                  <div>
                    <span className="text-[10px] font-semibold uppercase tracking-wider text-ink-faint">{t('context.stepLabel')}</span>
                    {/* Responsive grid: 5 columns when the sidebar is narrow,
                        10 when wider (5 divides 10, so wrapping rows stay
                        aligned both vertically and horizontally). */}
                    <div className="@container">
                      <div className="mt-1 grid grid-cols-5 gap-1 @[220px]:grid-cols-10">
                        {current.steps.map((s, si) => (
                          <button
                            key={s.step}
                            onClick={() => setSelectedStep(si)}
                            className={`text-[11px] px-1 py-0.5 rounded border transition-colors font-mono text-center truncate ${
                              si === selectedStep
                                ? 'border-accent/40 text-accent bg-accent/10'
                                : 'border-surface-border text-ink-soft hover:bg-surface-raised hover:text-ink'
                            }`}
                          >
                            {s.step}
                          </button>
                        ))}
                      </div>
                    </div>
                  </div>
                )}

                {/* Meta row */}
                <div className="flex items-center gap-2 text-[10px] text-ink-faint px-1">
                  <span className="font-mono">{t('context.metaTokens', { step: currentStep.step, tokens: currentStep.tokens_estimate })}</span>
                  <span>·</span>
                  <span>{(current.config as any)?.model ?? '?'}</span>
                  <span>·</span>
                  <span>{(current.config as any)?.mode ?? 'default'}</span>
                </div>

                {/* System prompt (collapsible) — hidden in raw mode (it's already
                    in the raw JSON as the first message). */}
                {!showRaw && current.system != null && (
                  <div className="rounded-md border border-surface-border bg-surface-raised/50">
                    <button
                      onClick={() => setSystemOpen((v) => !v)}
                      className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left"
                    >
                      <span className="text-[10px] text-ink-faint">{systemOpen ? '▾' : '▸'}</span>
                      <span className="text-[11px] font-semibold text-ink-soft">{t('context.system')}</span>
                      <span className="text-[10px] text-ink-faint ml-auto">{t('context.chars', { count: (current.system || '').length })}</span>
                    </button>
                    {systemOpen && (
                      <pre className="px-3 pb-2 text-[11px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft border-t border-surface-border pt-2">
                        {current.system}
                      </pre>
                    )}
                  </div>
                )}

                {/* Tools (collapsible) — hidden in raw mode (already in raw JSON). */}
                {!showRaw && current.tools.length > 0 && (
                  <div className="rounded-md border border-surface-border bg-surface-raised/50">
                    <button
                      onClick={() => setToolsOpen((v) => !v)}
                      className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left"
                    >
                      <span className="text-[10px] text-ink-faint">{toolsOpen ? '▾' : '▸'}</span>
                      <span className="text-[11px] font-semibold text-ink-soft">{t('context.tools')}</span>
                      <span className="text-[10px] text-ink-faint ml-auto">{current.tools.length}</span>
                    </button>
                    {toolsOpen && (
                      <div className="border-t border-surface-border px-2.5 py-2 space-y-1.5">
                        {current.tools.map((t, i) => {
                          const tr = t as Record<string, unknown>
                          const name = toolName(tr)
                          const desc = toolDescription(tr)
                          const schema = toolSchema(tr)
                          return (
                            <div key={i} className="rounded border border-surface-border bg-surface px-2 py-1.5">
                              <span className="text-[11px] font-mono font-semibold text-ink">{name}</span>
                              {desc && <p className="text-[10px] text-ink-soft mt-0.5 leading-relaxed">{desc}</p>}
                              {schema && (
                                <CollapsibleText text={schema} className="text-[10px] leading-relaxed whitespace-pre-wrap break-words text-ink-faint mt-1 font-mono" />
                              )}
                            </div>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )}

                {/* Messages */}
                {showRaw ? (
                  <pre className="rounded-md border border-surface-border bg-surface-raised/50 px-3 py-2 text-[11px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft overflow-x-auto font-mono">
                    {raw}
                  </pre>
                ) : (
                  <div className="space-y-1.5">
                    {currentStep.messages.map((m, i) => {
                      const role = (m.role as string) || '?'
                      const text = messageText(m as Record<string, unknown>)
                      const calls = messageToolCalls(m as Record<string, unknown>)
                      const keyBase = `${current.turn}-${i}`

                      // Flatten each provider message into one or more typed
                      // bubbles: an assistant message with tool_calls expands
                      // into its own "tool call" bubbles (its text, if any,
                      // stays an "assistant" bubble).
                      if (role === 'user') {
                        const source = (currentStep.sources?.[i]) ?? 'human'
                        // A compaction summary is a synthetic user message that
                        // replaced the compacted middle — label it distinctly so
                        // it's never mistaken for a real user turn.
                        if (source === 'compaction') {
                          const meta = currentStep.compactions?.find((c) => c.index === i)
                          return (
                            <CompactionBubble
                              key={keyBase}
                              summaryText={text}
                              meta={meta ?? { index: i, turn: current.turn, step: null, shadowed: [] }}
                              currentTurn={current.turn}
                            />
                          )
                        }
                        // Other synthetic injections (memory/skill/mode/
                        // interruption) are context, not user turns.
                        if (source !== 'human') {
                          return (
                            <Bubble key={keyBase} style={ROLE_STYLE.injection} meta={<span className="text-[10px] font-mono text-ink-faint truncate min-w-0 ml-1">{source}</span>}>
                              <CollapsibleText text={text} className="text-[11px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft mt-1" />
                            </Bubble>
                          )
                        }
                        return (
                          <Bubble key={keyBase} style={ROLE_STYLE.user}>
                            <CollapsibleText text={text} className="text-[11px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft mt-1" />
                          </Bubble>
                        )
                      }

                      if (role === 'assistant') {
                        const nodes: React.ReactNode[] = []
                        if (text.trim() !== '') {
                          nodes.push(
                            <Bubble key={`${keyBase}-text`} style={ROLE_STYLE.assistant}>
                              <CollapsibleText text={text} className="text-[11px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft mt-1" />
                            </Bubble>
                          )
                        } else if (calls.length > 0) {
                          // Text-less assistant message carrying only tool
                          // calls — keep a visible (collapsed) assistant marker
                          // so the role sequence stays readable.
                          nodes.push(
                            <Bubble key={`${keyBase}-marker`} style={ROLE_STYLE.assistant}>
                              <span className="text-[11px] text-ink-faint mt-1 block">{t('context.toolCallOnly')}</span>
                            </Bubble>
                          )
                        }
                        calls.forEach((c, j) => {
                          nodes.push(
                            <Bubble key={`${keyBase}-call-${j}`} style={ROLE_STYLE.toolCall} meta={<span className="text-[10px] font-mono truncate min-w-0" style={{ color: 'rgb(var(--role-toolcall))' }}>{c.name}</span>}>
                              <CollapsibleText text={prettyArgs(c.arguments)} className="text-[10px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft mt-1 font-mono" />
                            </Bubble>
                          )
                        })
                        return <React.Fragment key={keyBase}>{nodes}</React.Fragment>
                      }

                      if (role === 'tool') {
                        const cid = toolResultId(m as Record<string, unknown>)
                        return (
                          <Bubble key={keyBase} style={ROLE_STYLE.toolResult} meta={cid ? <span className="text-[10px] font-mono text-ink-faint truncate min-w-0 ml-1">{cid}</span> : undefined}>
                            {text.trim() !== '' ? (
                              <CollapsibleText text={text} className="text-[11px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft mt-1" />
                            ) : (
                              <span className="text-[11px] text-ink-faint mt-1 block">{t('context.empty')}</span>
                            )}
                          </Bubble>
                        )
                      }

                      // Unknown role — render generically.
                      return (
                        <Bubble key={keyBase} style={ROLE_STYLE.assistant}>
                          <CollapsibleText text={text || JSON.stringify(m)} className="text-[11px] leading-relaxed whitespace-pre-wrap break-words text-ink-soft mt-1" />
                        </Bubble>
                      )
                    })}
                  </div>
                )}

                {/* Tools + config summary */}
                <div className="flex items-center gap-2 text-[10px] text-ink-faint px-1">
                  <span>{t('context.toolsCount', { count: current.tools.length })}</span>
                  <span>·</span>
                  <span>{t('context.contextWindow', { value: (current.config as any)?.context_window ?? '?' })}</span>
                  <span>·</span>
                  <span>{t('context.maxTokens', { value: (current.config as any)?.max_tokens ?? '?' })}</span>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
