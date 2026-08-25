import React, { useState } from 'react'
import { useStore } from '../stores'
import type { ChatMessage, AgentNode } from '../../shared/types'
import { useT } from '../useI18n'

const STATUS_DOT: Record<AgentNode['status'] | 'root', string> = {
  running: 'bg-accent animate-pulse',
  done: 'bg-emerald-500',
  error: 'bg-red-500',
  root: 'bg-ink-soft',
}

// Visual logic-tree diagram of the agents in one turn. A synthetic "root" node
// represents the main agent; its children are the subagents whose parent_id is
// "root", recursing down. Rendered inside the AgentInspector panel; clicking a
// node selects it for the detail view, and clicking its chevron folds/unfolds
// that node's subtree.
export default function SubagentTree({ message }: { message: ChatMessage }) {
  const t = useT()
  const nodes = message.subagents || {}

  return (
    <div className="text-sm">
      <TreeNode
        message={message}
        agentId="root"
        label={t('tree.mainAgent')}
        subtitle={t('tree.root')}
        status="root"
        childIds={Object.values(nodes).filter((n) => n.parent_id === 'root').map((n) => n.agent_id)}
        isRoot
      />
    </div>
  )
}

function TreeNode({
  message, agentId, label, subtitle, status, childIds, isRoot = false,
}: {
  message: ChatMessage
  agentId: string
  label: string
  subtitle?: string
  status: AgentNode['status'] | 'root'
  childIds: string[]
  isRoot?: boolean
}) {
  const t = useT()
  const selected = useStore((s) => s.selectedAgent)
  const selectAgent = useStore((s) => s.selectAgent)
  const nodes = message.subagents || {}
  const isSelected = selected?.messageId === message.id && selected?.agentId === agentId
  const hasChildren = childIds.length > 0

  // Per-node fold state — defaults expanded so a running subtree streams into
  // view. Persists across re-renders (the node stays mounted, keyed by id) so
  // a manual fold isn't undone when sibling nodes update mid-stream.
  const [collapsed, setCollapsed] = useState(false)

  return (
    <div>
      {/* Two sibling buttons (chevron + select) rather than nested buttons,
          which is invalid HTML. Selection/hover styling lives on the wrapper. */}
      <div
        className={`flex items-center rounded transition-colors ${
          isSelected ? 'bg-sidebar-active ring-1 ring-accent/60' : 'hover:bg-surface-raised'
        }`}
      >
        {hasChildren ? (
          <button
            onClick={() => setCollapsed((v) => !v)}
            className="flex-shrink-0 w-5 self-stretch flex items-center justify-center text-ink-faint hover:text-ink transition-colors"
            title={collapsed ? t('tree.expand') : t('tree.collapse')}
          >
            <span className={`text-[10px] transition-transform ${collapsed ? '' : 'rotate-90'}`}>▶</span>
          </button>
        ) : (
          <span className="flex-shrink-0 w-5" />
        )}
        <button
          onClick={() => selectAgent({ messageId: message.id, agentId })}
          className="flex-1 min-w-0 flex items-center gap-2 pr-2 py-1.5 text-left"
        >
          <span className={`inline-block w-2 h-2 rounded-full flex-shrink-0 ${STATUS_DOT[status]}`} />
          <span className={`text-xs font-mono flex-shrink-0 ${isRoot ? 'text-ink font-semibold' : 'text-ink-soft'}`}>{label}</span>
          {collapsed && hasChildren && (
            <span className="text-[10px] text-ink-faint flex-shrink-0">({childIds.length})</span>
          )}
          {subtitle && !collapsed && (
            <span className="text-[11px] text-ink-faint truncate flex-1 min-w-0">{subtitle}</span>
          )}
        </button>
      </div>

      {hasChildren && !collapsed && (
        <div className="ml-2.5 pl-2.5 border-l border-surface-border space-y-0.5 mt-0.5">
          {childIds.map((cid) => {
            const child = nodes[cid]
            if (!child) return null
            const grandIds = Object.values(nodes).filter((n) => n.parent_id === cid).map((n) => n.agent_id)
            return (
              <TreeNode
                key={cid}
                message={message}
                agentId={cid}
                label={child.agent_id.slice(0, 8)}
                subtitle={child.description}
                status={child.status}
                childIds={grandIds}
              />
            )
          })}
        </div>
      )}
    </div>
  )
}
