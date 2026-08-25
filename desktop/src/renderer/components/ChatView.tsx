import React, { useEffect, useRef } from 'react'
import { useStore } from '../stores'
import MessageBubble from './MessageBubble'
import PermissionCard from './PermissionCard'
import BatchEditCard from './BatchEditCard'
import SubagentFocusView from './SubagentFocusView'
import { useT } from '../useI18n'

// Reconstruct markdown source for the current selection by collecting the
// `data-md` of every TextChunk the selection intersects (see MessageBubble).
// Falls back to plain text when the selection touches no tagged chunk.
function markdownForSelection(sel: Selection | null): string {
  if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return ''
  const range = sel.getRangeAt(0)
  const chunks = document.querySelectorAll<HTMLElement>('[data-md]')
  const parts: string[] = []
  chunks.forEach((el) => {
    if (range.intersectsNode(el)) {
      const md = el.getAttribute('data-md')
      if (md) parts.push(md)
    }
  })
  return parts.join('\n\n')
}

const SCROLL_NEAR_BOTTOM_PX = 80  // threshold: treat as "at bottom" within 80px

export default function ChatView() {
  const t = useT()
  const messages = useStore((s) => s.messages)
  const isStreaming = useStore((s) => s.isStreaming)
  const openContextMenu = useStore((s) => s.openContextMenu)
  const focusAgent = useStore((s) => s.focusAgent)

  const scrollRef = useRef<HTMLDivElement>(null)
  const bottomRef = useRef<HTMLDivElement>(null)
  const userScrolledAway = useRef(false)

  // Detect user scrolls — if they pull up away from the bottom, disable
  // auto-scroll until they explicitly go back to the bottom.
  const handleScroll = () => {
    const el = scrollRef.current
    if (!el) return
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight
    userScrolledAway.current = dist > SCROLL_NEAR_BOTTOM_PX
  }

  // Auto-scroll on new content only when the user hasn't scrolled away.
  useEffect(() => {
    if (!userScrolledAway.current) {
      bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
    }
  }, [messages, isStreaming])

  const onContextMenu = (e: React.MouseEvent) => {
    const sel = window.getSelection()
    const selection = sel && !sel.isCollapsed ? sel.toString() : ''
    // Only show the menu when there's a text selection — matches the ask
    // (Copy / Copy as Markdown operate on selected text).
    if (!selection) return
    e.preventDefault()
    openContextMenu({
      x: e.clientX,
      y: e.clientY,
      selection,
      markdown: markdownForSelection(sel) || selection,
    })
  }

  // A focused subagent replaces the parent transcript with its read-only
  // conversation view (breadcrumb + prompt + blocks). The parent messages stay
  // in the store so clearing focus returns here instantly.
  if (focusAgent) {
    return <SubagentFocusView />
  }

  if (messages.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-ink-faint">
        <div className="text-center">
          <h2 className="text-xl mb-2 text-ink-soft">{t('chat.emptyTitle')}</h2>
          <p className="text-sm">{t('chat.emptyHint')}</p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 overflow-y-auto px-4 py-4 select-text pl-14" ref={scrollRef} onScroll={handleScroll} onContextMenu={onContextMenu} data-scroll-container>
      {messages.map((msg, i) => (
        <MessageBubble
          key={msg.id}
          message={msg}
          isStreaming={isStreaming && msg === messages[messages.length - 1] && msg.role === 'agent'}
          isLastAgent={msg.role === 'agent' && i === messages.length - 1}
        />
      ))}
      <PermissionCard />
      <BatchEditCard />
      <div ref={bottomRef} />
    </div>
  )
}
