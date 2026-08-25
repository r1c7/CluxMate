import React, { useState } from 'react'
import { useStore } from '../stores'
import type { AgentNode, ChatMessage } from '../../shared/types'
import { BlockList, AnswerBlock } from './MessageBubble'
import SubagentTree from './SubagentTree'
import { useT } from '../useI18n'

const STATUS_STYLE: Record<AgentNode['status'], { dot: string; labelKey: string }> = {
  running: { dot: 'bg-accent animate-pulse', labelKey: 'inspector.status.running' },
  done: { dot: 'bg-emerald-500', labelKey: 'inspector.status.done' },
  error: { dot: 'bg-red-500', labelKey: 'inspector.status.error' },
}

// Right-side panel: a logic-tree diagram of the turn's agents on top, and the
// selected node's detail (activity + result) below. Opened by the "Agent Tree"
// button in the result area; collapses to width 0 when nothing is selected
// (handled by the App wrapper).
export default function AgentInspector() {
  const t = useT()
  const selected = useStore((s) => s.selectedAgent)
  const messages = useStore((s) => s.messages)
  const selectAgent = useStore((s) => s.selectAgent)

  const message: ChatMessage | undefined = selected
    ? messages.find((m) => m.id === selected.messageId)
    : undefined

  if (!selected || !message) return null

  const isRoot = selected.agentId === 'root'
  const node: AgentNode | undefined = isRoot ? undefined : message.subagents?.[selected.agentId]

  // The root agent's "prompt" is the user message that triggered this turn —
  // the message directly preceding it in the transcript.
  const idx = messages.findIndex((m) => m.id === message.id)
  const rootPrompt = idx > 0 && messages[idx - 1]?.role === 'user'
    ? messages[idx - 1].content
    : ''

  return (
    <div className="h-full flex flex-col bg-surface">
      <div className="flex items-center gap-2 px-3 h-9 border-b border-surface-border flex-shrink-0">
        <span className="text-xs font-semibold text-ink-soft">{t('inspector.agentTree')}</span>
        <button
          onClick={() => selectAgent(null)}
          className="text-ink-faint hover:text-ink text-base px-1 ml-auto transition-colors"
          title={t('inspector.close')}
        >×</button>
      </div>

      {/* Tree diagram */}
      <div className="px-2 py-2 border-b border-surface-border flex-shrink-0 max-h-[45%] overflow-y-auto">
        <SubagentTree message={message} />
      </div>

      {/* Selected-node detail */}
      <div className="flex-1 overflow-y-auto px-3 py-3 space-y-2">
        {isRoot ? (
          <RootDetail message={message} prompt={rootPrompt} />
        ) : node ? (
          <NodeDetail node={node} messageId={message.id} />
        ) : (
          <p className="text-xs text-ink-faint">{t('inspector.nodeMissing')}</p>
        )}
      </div>
    </div>
  )
}

// The prompt an agent was launched with — light italic, clearly set apart from
// the model's answer below it. Collapsible, collapsed by default.
function PromptBlock({ prompt }: { prompt: string }) {
  const t = useT()
  const [open, setOpen] = useState(false)
  if (!prompt) return null
  return (
    <div className="rounded-md border border-surface-border bg-surface-raised/40 overflow-hidden">
      <button
        onClick={() => setOpen((v) => !v)}
        className="w-full flex items-center gap-2 px-2.5 py-1.5 text-left cursor-pointer hover:bg-surface-raised transition-colors"
      >
        <span className="text-[10px] uppercase tracking-wide text-ink-faint/70">{t('inspector.prompt')}</span>
        {!open && (
          <span className="text-xs italic text-ink-faint truncate flex-1 min-w-0">{prompt}</span>
        )}
        <span className={`text-ink-faint text-[10px] flex-shrink-0 ml-auto transition-transform ${open ? 'rotate-90' : ''}`}>▶</span>
      </button>
      {open && (
        <div className="border-t border-surface-border px-2.5 py-2">
          <p className="text-xs italic text-ink-faint whitespace-pre-wrap break-words">{prompt}</p>
        </div>
      )}
    </div>
  )
}

function RootDetail({ message, prompt }: { message: ChatMessage; prompt: string }) {
  const t = useT()
  const isStreaming = useStore((s) => s.isStreaming)
  const toggleContext = useStore((s) => s.toggleContext)
  const blocks = message.blocks
    ?? (message.content ? [{ type: 'text' as const, text: message.content }] : [])
  return (
    <>
      <div className="flex items-center gap-2">
        <span className="inline-block w-2 h-2 rounded-full bg-ink-soft flex-shrink-0" />
        <span className="text-xs font-mono font-semibold text-ink">{t('inspector.mainAgent')}</span>
      </div>
      {/* Same entry as the header button, offered inline so both the main agent
          and any subagent node expose their context history from the tree. */}
      <button
        onClick={() => toggleContext(true)}
        className="w-full text-xs px-2.5 py-1.5 rounded-md bg-accent/15 text-accent font-semibold border border-accent/40 hover:bg-accent/25 hover:border-accent/60 transition-colors text-left"
        title={t('inspector.viewContextTitle')}
      >
        {t('inspector.viewContext')}
      </button>
      <PromptBlock prompt={prompt} />
      {message.thinking && (
        <details className="mb-1">
          <summary className="text-xs text-ink-faint cursor-pointer italic">{isStreaming ? t('inspector.thinking') : t('inspector.thought')}</summary>
          <p className="text-xs text-ink-faint italic mt-1 whitespace-pre-wrap">{message.thinking}</p>
        </details>
      )}
      {blocks.length > 0 ? (
        <AnswerBlock defaultOpen={true}>
          <div className="space-y-1.5"><BlockList blocks={blocks} /></div>
        </AnswerBlock>
      ) : (
        <p className="text-xs text-ink-faint">{t('inspector.noContent')}</p>
      )}
    </>
  )
}

function NodeDetail({ node, messageId }: { node: AgentNode; messageId: string }) {
  const t = useT()
  const status = STATUS_STYLE[node.status]
  const viewSubagentContext = useStore((s) => s.viewSubagentContext)
  const focusSubagent = useStore((s) => s.focusSubagent)
  return (
    <>
      <div className="flex items-center gap-2">
        <span className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${status.dot}`} />
        <span className="text-xs font-mono font-semibold text-ink-soft">{node.agent_id.slice(0, 8)}</span>
        <span className="text-[10px] text-ink-faint ml-auto">{t(status.labelKey)} · {t('inspector.depth', { depth: node.depth })}</span>
      </div>
      {/* Full-width read-only view of this subagent's conversation in the MAIN
          area, with a lineage breadcrumb (Root → sub → sub-sub). Closes the tree
          so the node isn't shown in both places at once. */}
      <button
        onClick={() => focusSubagent(messageId, node.agent_id)}
        className="w-full text-xs px-2.5 py-1.5 rounded-md bg-accent/15 text-accent font-semibold border border-accent/40 hover:bg-accent/25 hover:border-accent/60 transition-colors text-left"
        title={t('inspector.showMainTitle')}
      >
        {t('inspector.showMain')}
      </button>
      {/* Each subagent logs its own turn-by-turn context to <agent_id>.jsonl —
          jump straight to that context (this closes the tree, same dock slot). */}
      <button
        onClick={() => viewSubagentContext(messageId, node.agent_id, node.subagent_type)}
        className="w-full text-xs px-2.5 py-1.5 rounded-md bg-accent/15 text-accent font-semibold border border-accent/40 hover:bg-accent/25 hover:border-accent/60 transition-colors text-left"
        title={t('inspector.viewContextSubTitle')}
      >
        {t('inspector.viewContext')}
      </button>
      {node.prompt ? (
        <PromptBlock prompt={node.prompt} />
      ) : node.description ? (
        <p className="text-xs text-ink-faint italic">{node.description}</p>
      ) : null}

      {node.thinking && (
        <details className="mb-1">
          <summary className="text-xs text-ink-faint cursor-pointer italic">{node.status === 'running' ? t('inspector.thinking') : t('inspector.thought')}</summary>
          <p className="text-xs text-ink-faint italic mt-1 whitespace-pre-wrap">{node.thinking}</p>
        </details>
      )}
      {node.blocks.length > 0 ? (
        <AnswerBlock defaultOpen={true}>
          <div className="space-y-1.5"><BlockList blocks={node.blocks} /></div>
        </AnswerBlock>
      ) : node.status === 'error' && node.result ? (
        <div className="mt-3 pt-3 border-t border-surface-border">
          <div className="text-[10px] uppercase tracking-wide text-ink-faint mb-1">{t('inspector.error')}</div>
          <pre className="text-[11px] font-mono text-red-600 whitespace-pre-wrap break-words">{node.result}</pre>
        </div>
      ) : (
        <p className="text-xs text-ink-faint">{t('inspector.noActivity')}</p>
      )}
    </>
  )
}
