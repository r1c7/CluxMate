import React, { useEffect } from 'react'
import { useStore } from '../stores'
import type { AgentNode, MessageBlock } from '../../shared/types'
import { BlockList, AnswerBlock } from './MessageBubble'
import { useT } from '../useI18n'

// Short, stable id prefix for breadcrumb chips — the full agent_id is 32 hex
// chars and would overwhelm the chain.
function shortId(id: string): string {
  return id.length > 8 ? id.slice(0, 8) : id
}

// Walk `parent_id` links from the focused node back up to the synthetic "root",
// returning the chain root-most-first (the focused node is last). The synthetic
// root (the parent/main agent) is NOT in the list — the breadcrumb renders it
// separately as "Root Agent".
function lineage(nodes: Record<string, AgentNode>, agentId: string): AgentNode[] {
  const chain: AgentNode[] = []
  const seen = new Set<string>()
  let id: string | undefined = agentId
  while (id && !seen.has(id)) {
    seen.add(id)
    const n: AgentNode | undefined = nodes[id]
    if (!n) break
    chain.unshift(n)
    id = n.parent_id && n.parent_id !== 'root' ? n.parent_id : undefined
  }
  return chain
}

// Read-only, full-width view of one subagent's conversation, shown in the MAIN
// area instead of the parent transcript. A lineage breadcrumb across the top
// ("Root Agent → sub → sub-sub") traces where the agent came from; clicking any
// node switches focus, and "Root Agent" / the footer button return to the parent.
export default function SubagentFocusView() {
  const t = useT()
  const focus = useStore((s) => s.focusAgent)
  const messages = useStore((s) => s.messages)
  const clearFocus = useStore((s) => s.clearFocus)
  const focusSubagent = useStore((s) => s.focusSubagent)
  const openAgentTreeFromFocus = useStore((s) => s.openAgentTreeFromFocus)

  const message = focus ? messages.find((m) => m.id === focus.messageId) : undefined
  const nodes = message?.subagents || {}
  const node = focus ? nodes[focus.agentId] : undefined

  // Defensive: if the focused node vanished (e.g. a stale focus after a switch),
  // fall back to the parent transcript rather than rendering a blank area.
  useEffect(() => {
    if (focus && (!message || !node)) clearFocus()
  }, [focus, message, node, clearFocus])

  if (!focus || !message || !node) return null

  const chain = lineage(nodes, focus.agentId)

  // A subagent's "conversation" is its task prompt (the human message that
  // launched it) followed by its interleaved text + tool output. `blocks` already
  // includes the final text block, so `result` is only a fallback for error
  // completions that produced no assistant text.
  const blocks: MessageBlock[] = node.blocks.length > 0
    ? node.blocks
    : node.result
      ? [{ type: 'text', text: node.result }]
      : []

  return (
    <div className="flex-1 flex flex-col min-h-0 min-w-0">
      {/* Lineage breadcrumb — a clean, clickable trail from the root agent to the
          focused subagent. Nodes are quiet text that reveal a soft accent pill on
          hover; the current node is a filled pill. The chain scrolls horizontally.
          Uses `bg-sidebar` (chrome) so it never reads as a chat bubble. */}
      <div className="flex items-center gap-1 px-3 h-14 border-b border-surface-border bg-sidebar flex-shrink-0 min-w-0 select-none">
        <button
          onClick={clearFocus}
          className="flex-shrink-0 inline-flex items-center font-mono text-sm px-2 py-1 rounded-md border border-transparent text-ink-soft hover:text-accent hover:bg-accent-muted hover:border-accent/20 transition-colors"
          title={t('focus.returnTitle')}
        >
          <span className="font-medium">{t('focus.rootAgent')}</span>
        </button>

        <div className="flex items-center gap-0.5 overflow-x-auto whitespace-nowrap min-w-0 flex-1">
          {chain.map((n) => {
            const active = n.agent_id === focus.agentId
            return (
              <React.Fragment key={n.agent_id}>
                <svg
                  aria-hidden
                  className="flex-shrink-0 mx-0.5 w-4 h-4 text-ink-soft"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.5"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M5 12h14" />
                  <path d="m13 6 6 6-6 6" />
                </svg>
                <button
                  onClick={() => !active && focusSubagent(message.id, n.agent_id)}
                  disabled={active}
                  className={`flex-shrink-0 inline-flex items-center text-sm px-2 py-1 rounded-md border transition-colors ${
                    active
                      ? 'bg-accent-muted text-accent border-accent/30 font-medium'
                      : 'border-transparent text-ink-soft hover:text-accent hover:bg-accent-muted hover:border-accent/20'
                  }`}
                  title={active ? t('focus.current', { id: n.agent_id }) : t('focus.clickToView', { id: n.agent_id })}
                >
                  <span className="font-mono">{shortId(n.agent_id)}</span>
                </button>
              </React.Fragment>
            )
          })}
        </div>
      </div>

      {/* Actions row — below the breadcrumb, top-left of the conversation area.
          Kept out of the breadcrumb so the lineage path stays a clean trail. */}
      <div className="flex items-center gap-2 px-3 py-2 flex-shrink-0">
        <button
          onClick={openAgentTreeFromFocus}
          className="inline-flex items-center gap-1.5 text-sm px-2.5 py-1.5 rounded-md border border-surface-border text-ink-soft hover:text-accent hover:border-accent/40 hover:bg-accent-muted transition-colors"
          title={t('focus.openTreeTitle')}
        >
          <span aria-hidden className="text-[15px] leading-none">⌗</span>
          <span className="font-medium">{t('focus.agentTree')}</span>
        </button>

        <button
          onClick={clearFocus}
          className="inline-flex items-center text-sm px-3 py-1.5 rounded-md bg-accent text-accent-ink hover:bg-accent-hover shadow-sm transition-colors"
          title={t('focus.returnTitle')}
        >
          <span className="font-medium">{t('focus.backToMain')}</span>
        </button>
      </div>

      {/* Conversation body */}
      <div className="flex-1 overflow-y-auto px-4 py-4 select-text">
        {/* The task prompt that launched this subagent, rendered as a user
            bubble so the "who asked what" shape matches the main transcript. */}
        {node.prompt ? (
          <div className="flex flex-col mb-4">
            <div className="self-end text-xs text-ink-faint/40 mb-0.5">{t('focus.taskPrompt')}</div>
            <div className="self-end max-w-[85%] rounded-2xl rounded-br-sm px-4 py-2.5 bg-chat-user text-ink">
              <p className="whitespace-pre-wrap break-words text-sm">{node.prompt}</p>
            </div>
          </div>
        ) : null}

        {node.thinking && (
          <details className="mb-1">
            <summary className="text-xs text-ink-faint cursor-pointer italic">{t('focus.thought')}</summary>
            <p className="text-xs text-ink-faint italic mt-1 whitespace-pre-wrap break-words">{node.thinking}</p>
          </details>
        )}

        <div className="flex justify-start mb-4 min-w-0">
          <div className="max-w-[85%] w-full min-w-0 space-y-1.5">
            {blocks.length > 0 ? (
              <AnswerBlock defaultOpen={true}>
                <BlockList blocks={blocks} />
              </AnswerBlock>
            ) : node.status === 'running' ? (
              <span className="inline-block w-2 h-4 bg-accent animate-pulse align-middle" />
            ) : (
              <p className="text-xs text-ink-faint">{t('focus.noActivity')}</p>
            )}
          </div>
        </div>
      </div>

      {/* Read-only footer — this is a navigation view, not a resumable session. */}
      <div className="flex items-center gap-2 px-3 h-8 border-t border-surface-border flex-shrink-0">
        <span className="text-[10px] text-ink-faint">{t('focus.readonly')}</span>
      </div>
    </div>
  )
}
