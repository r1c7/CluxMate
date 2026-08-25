import React, { useState, useCallback } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { ChatMessage, MessageBlock } from '../../shared/types'
import DiffCard from './DiffCard'
import ToolCallCard from './ToolCallCard'
import ChangedFilesCard from './ChangedFilesCard'
import { useStore } from '../stores'
import { useT } from '../useI18n'
import { formatTime } from '../../shared/format-time'

interface Props {
  message: ChatMessage
  isStreaming: boolean
  isLastAgent?: boolean
}

// Button shown in the result area only when the turn spawned subagents. Clicking
// it opens the right-side logic-tree panel (selecting the root agent). When no
// subagents exist, nothing renders and the reply shows as normal.
function SubagentTreeButton({ message }: { message: ChatMessage }) {
  const t = useT()
  const count = message.subagents ? Object.keys(message.subagents).length : 0
  const selected = useStore((s) => s.selectedAgent)
  const selectAgent = useStore((s) => s.selectAgent)
  if (count === 0) return null
  const isOpen = selected?.messageId === message.id

  return (
    <button
      onClick={() => selectAgent(isOpen ? null : { messageId: message.id, agentId: 'root' })}
      className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs transition-colors ${
        isOpen
          ? 'border-accent/60 bg-accent/15 text-accent'
          : 'border-surface-border bg-surface-raised/60 text-ink-soft hover:bg-surface-raised hover:text-ink'
      }`}
      title={t('chat.agentTreeTitle')}
    >
      <span className="text-[13px] leading-none">⌗</span>
      <span>{t('chat.agentTree')}</span>
      <span className="text-[10px] text-ink-faint">{t('chat.subAgents', { count, plural: count === 1 ? '' : 's' })}</span>
    </button>
  )
}

// Flatten a react node tree back to its plain-text content (for copy + line
// counting). Code content arrives as a string, but stay defensive.
function nodeText(node: React.ReactNode): string {
  if (typeof node === 'string') return node
  if (typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(nodeText).join('')
  if (React.isValidElement(node)) {
    return nodeText((node.props as { children?: React.ReactNode }).children)
  }
  return ''
}

// Fenced code block with a header bar: language label, line count, copy, and a
// collapse toggle. Overrides ReactMarkdown's default <pre> — inline code has no
// <pre> wrapper, so it is unaffected. Long blocks (>30 lines) mount collapsed;
// because state persists across re-renders, a block streaming in live starts
// short (expanded) and stays open as it grows, while a long block loaded from
// history mounts already collapsed.
const COLLAPSE_LINES = 30
function CodeBlock({ children }: React.ComponentPropsWithoutRef<'pre'>) {
  const t = useT()
  const codeEl = React.Children.toArray(children).find(React.isValidElement) as
    | React.ReactElement<{ className?: string; children?: React.ReactNode }>
    | undefined
  const lang = /language-(\w+)/.exec(codeEl?.props.className || '')?.[1] || ''
  const raw = nodeText(codeEl?.props.children).replace(/\n+$/, '')
  const lineCount = raw ? raw.split('\n').length : 0

  const [collapsed, setCollapsed] = useState(() => lineCount > COLLAPSE_LINES)
  const [copied, setCopied] = useState(false)
  const handleCopy = useCallback(() => {
    if (!raw) return
    window.electronAPI.writeClipboard(raw)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [raw])

  return (
    <div className="not-prose my-2 rounded-md border border-surface-border bg-surface-raised overflow-hidden">
      {/* select-none keeps the header out of text selections so a plain Ctrl+C
          over the block copies only code, not the button labels. */}
      <div className="flex items-center gap-2 px-2.5 py-1 border-b border-surface-border select-none">
        <button
          onClick={() => setCollapsed((v) => !v)}
          className="flex items-center gap-1.5 text-ink-soft hover:text-ink transition-colors"
          title={collapsed ? t('code.expand') : t('code.collapse')}
        >
          <span className={`text-[10px] text-ink-faint transition-transform ${collapsed ? '' : 'rotate-90'}`}>▶</span>
          <span className="text-[11px] font-mono">{lang || t('code.codeFallback')}</span>
        </button>
        <span className="text-[10px] text-ink-faint">{t('code.lines', { count: lineCount, plural: lineCount === 1 ? '' : 's' })}</span>
        <button
          onClick={handleCopy}
          className="ml-auto text-[11px] text-ink-faint hover:text-ink transition-colors"
          title={t('code.copyCode')}
        >
          {copied ? <span className="text-accent">{t('code.copied')}</span> : t('code.copy')}
        </button>
      </div>
      {collapsed ? (
        <button
          onClick={() => setCollapsed(false)}
          className="w-full text-left px-3 py-1.5 text-[11px] font-mono text-ink-faint hover:bg-surface hover:text-ink-soft transition-colors truncate"
        >
          {raw.split('\n')[0] || t('code.empty')}…
        </button>
      ) : (
        <pre className="text-[12px] font-mono text-ink overflow-x-auto px-3 py-2 m-0 whitespace-pre">
          {children}
        </pre>
      )}
    </div>
  )
}

const MARKDOWN_COMPONENTS = { pre: CodeBlock }

// Collapsible answer wrapper with an "Answer" label header. Used by both the
// root message bubble and the subagent inspector so answers look consistent.
// Uses a native <details> element so the disclosure triangle matches Thought.
export function AnswerBlock({ children, defaultOpen = true }: { children: React.ReactNode; defaultOpen?: boolean }) {
  const t = useT()
  return (
    <details open={defaultOpen}>
      <summary className="text-xs text-ink-faint cursor-pointer">{t('chat.answer')}</summary>
      <div className="mt-0.5">{children}</div>
    </details>
  )
}

// Memoized: markdown parsing is the single most expensive per-render cost, and
// during streaming every text_delta re-renders the live message. Without memo,
// ALL text blocks (incl. completed ones) re-parse their full markdown on each
// delta — O(n²) over a long reply, which saturated the main thread and froze
// the UI (Stop / session-switch clicks couldn't be processed). With memo, only
// the one growing block re-parses; every settled block is skipped by ref-equal
// `text`. Combined with the memoized MessageBubble, non-active messages don't
// re-render at all mid-stream.
export const TextChunk = React.memo(function TextChunk({ text }: { text: string }) {
  if (!text) return null
  return (
    // data-md carries this chunk's raw markdown source so the right-click
    // "Copy as Markdown" can reconstruct markdown from a DOM selection.
    <div
      data-md={text}
      className="prose prose-sm max-w-none prose-code:text-accent prose-a:text-accent text-ink"
    >
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={MARKDOWN_COMPONENTS}>{text}</ReactMarkdown>
      {text.includes('```diff') && <DiffCard content={text} />}
    </div>
  )
})

// Shared renderer for an ordered block list (text + tool cards). Used by the
// root message body and the subagent inspector so both look identical.
export function BlockList({ blocks }: { blocks: MessageBlock[] }) {
  return (
    <>
      {blocks.map((b, i) =>
        b.type === 'text'
          ? <TextChunk key={i} text={b.text} />
          : <ToolCallCard key={b.tool.call_id} tc={b.tool} />
      )}
    </>
  )
}

function MessageBubbleInner({ message, isStreaming, isLastAgent }: Props) {
  const t = useT()
  const isUser = message.role === 'user'
  const undoMessage = useStore((s) => s.undoMessage)
  const retryMessage = useStore((s) => s.retryMessage)
  const anyStreaming = useStore((s) => s.isStreaming)

  if (isUser) {
    // Undo is offered only when this message carries an anchor (checkpoints were
    // available when sent) and no turn is currently streaming — undoing mid-turn
    // would race the in-flight run.
    const canUndo = !!message.undo && !anyStreaming
    return (
      <div className="flex flex-col mb-4" data-msg-id={message.id}>
        <div className="self-end text-xs text-ink-faint/40 mb-0.5">{formatTime(message.timestamp)}</div>
        <div className="self-end max-w-[85%] rounded-2xl rounded-br-sm px-4 py-2.5 bg-chat-user text-ink">
          <p className="whitespace-pre-wrap text-sm">{message.content}</p>
        </div>
        {canUndo && (
          <div className="self-end">
            <button
              onClick={() => undoMessage(message.id)}
              className="mt-1 inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] text-ink-faint hover:text-ink hover:bg-surface-raised transition-colors"
              title={t('chat.undoTitle')}
            >
              <span className="text-[12px] leading-none">↶</span>
              <span>{t('chat.undo')}</span>
            </button>
          </div>
        )}
      </div>
    )
  }

  // Ordered blocks (text/tool interleaved) are the source of truth for agent
  // messages. Fall back to flat content for older messages without blocks.
  const blocks = message.blocks
    ?? (message.content ? [{ type: 'text' as const, text: message.content }] : [])
  const isEmpty = blocks.length === 0

  // Concatenate raw markdown source from all text blocks for copying.
  const rawText = blocks.filter((b): b is Extract<MessageBlock, { type: 'text' }> => b.type === 'text')
    .map((b) => b.text).join('') || message.content || ''

  const [copied, setCopied] = useState(false)
  const handleCopy = useCallback(() => {
    if (!rawText) return
    window.electronAPI.writeClipboard(rawText)
    setCopied(true)
    setTimeout(() => setCopied(false), 2000)
  }, [rawText])

  return (
    <div className="flex justify-start mb-4 group/message" data-msg-id={message.id}>
      <div className="max-w-[85%] w-full space-y-1.5">
        {message.thinking && (
          <details className="mb-1">
            <summary className="text-xs text-ink-faint cursor-pointer italic">{isStreaming ? t('chat.thinking') : t('chat.thought')}</summary>
            <p className="text-xs text-ink-faint italic mt-1 whitespace-pre-wrap">{message.thinking}</p>
          </details>
        )}

        <AnswerBlock defaultOpen={!isStreaming || blocks.length > 0}>
          <BlockList blocks={blocks} />

          {message.skillsUsed && message.skillsUsed.length > 0 && (
            <div className="space-y-0.5">
              {message.skillsUsed.map((sk) => (
                <div key={sk.slug} className="text-xs text-ink-faint italic flex items-center gap-1">
                  <span>{sk.trigger === 'command' ? t('chat.usedSkillManual', { name: sk.name }) : t('chat.usedSkill', { name: sk.name })}</span>
                </div>
              ))}
            </div>
          )}

          {message.hooksUsed && message.hooksUsed.length > 0 && (
            <div className="space-y-0.5">
              {message.hooksUsed.map((h, i) => {
                const cmd = (h.command.split(/[\\/]/).pop() || h.command).trim()
                const outcome = h.error != null
                  ? t('chat.hook.failed')
                  : h.blocked
                    ? (h.tool_name ? `${t('chat.hook.blocked')} ${h.tool_name}` : t('chat.hook.blocked'))
                    : h.feedback_count > 0
                      ? t('chat.hook.injected', { count: h.feedback_count })
                      : t('chat.hook.ran')
                return (
                  <div key={`${h.event}-${i}`} className="text-xs text-ink-faint italic flex items-center gap-1">
                    <span className="not-italic">↪</span>
                    <span>{h.event} · {cmd} · {outcome}</span>
                  </div>
                )
              })}
            </div>
          )}

          {message.turnDiff && message.turnDiff.files.length > 0 && (
            <ChangedFilesCard
              checkpointId={message.turnDiff.checkpoint_id}
              files={message.turnDiff.files}
              label={t('chat.filesChanged', { count: message.turnDiff.files.length, plural: message.turnDiff.files.length === 1 ? '' : 's' })}
            />
          )}

          {/* Timestamp (always visible) + Cache hit rate + Retry + Copy (hover-reveal). */}
          <div className="flex items-center gap-2 pt-1">
            <span className="text-xs text-ink-faint/40">{formatTime(message.timestamp)}</span>
            {!isStreaming && message.cacheUsage && message.cacheUsage.input_tokens > 0 && (
              (() => {
                const { input_tokens, cache_read, cache_write } = message.cacheUsage
                const pct = Math.round((cache_read / input_tokens) * 100)
                const tooltip = cache_read > 0
                  ? t('chat.cacheTooltip.cached', { cached: cache_read.toLocaleString(), total: input_tokens.toLocaleString() })
                  : cache_write > 0
                    ? t('chat.cacheTooltip.written', { written: cache_write.toLocaleString(), total: input_tokens.toLocaleString() })
                    : t('chat.cacheTooltip.total', { total: input_tokens.toLocaleString() })
                return (
                  <span
                    className={`text-xs px-1.5 py-0.5 rounded ${cache_read > 0 ? 'bg-emerald-500/10 text-emerald-700' : cache_write > 0 ? 'bg-amber-500/10 text-amber-700' : 'bg-surface-raised text-ink-faint'}`}
                    title={tooltip}
                  >
                    {cache_read > 0 ? t('chat.cacheChip.cached', { pct }) : cache_write > 0 ? t('chat.cacheChip.written') : t('chat.cacheChip.tokens', { count: input_tokens.toLocaleString() })}
                  </span>
                )
              })()
            )}
            {!isStreaming && isLastAgent && (
              <button
                onClick={() => retryMessage(message.id)}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-ink-faint hover:text-ink hover:bg-surface-raised transition-colors opacity-0 group-hover/message:opacity-100"
                title={t('chat.retryTitle')}
              >
                <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <polyline points="1 4 1 10 7 10"/>
                  <path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>
                </svg>
                <span>{t('chat.retry')}</span>
              </button>
            )}
            {!isStreaming && rawText && (
              <button
                onClick={handleCopy}
                className="inline-flex items-center gap-1 px-2 py-1 rounded text-xs text-ink-faint hover:text-ink hover:bg-surface-raised transition-colors opacity-0 group-hover/message:opacity-100"
                title={t('chat.copyRawTitle')}
              >
                {copied ? (
                  <span className="text-accent text-[11px]">{t('chat.copied')}</span>
                ) : (
                  <>
                    <svg className="w-3.5 h-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                      <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                    </svg>
                    <span>{t('chat.copy')}</span>
                  </>
                )}
              </button>
            )}
          </div>
        </AnswerBlock>

        {/* Subagent logic-tree toggle: always visible (self-hides when the turn
            spawned no subagents), including mid-stream so a running subagent's
            live output stays reachable while it works. */}
        <SubagentTreeButton message={message} />

        {isStreaming && isEmpty && (
          <span className="inline-block w-2 h-4 bg-accent animate-pulse align-middle" />
        )}
      </div>
    </div>
  )
}

// Memoized so a store update for the streaming message doesn't re-render every
// other (unchanged) bubble in the list. The store mutates messages immutably —
// only the changed message gets a new ref — so default shallow prop comparison
// correctly skips the rest. Components that need live store slices still read
// them via useStore internally, so memo doesn't stale them.
const MessageBubble = React.memo(MessageBubbleInner)
export default MessageBubble
