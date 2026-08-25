import React, { useState, useMemo, useRef, useLayoutEffect, useCallback } from 'react'
import { useStore } from '../stores'
import type { ChatMessage } from '../../shared/types'
import { useT } from '../useI18n'

const MARKER_WIDTH = 14
const MARKER_HEIGHT = 2
const MARKER_GAP = 8
const STRIP_PAD_LEFT = 4

// Maximum possible visual width of an expanded marker: 14px × 3x scale = 42px.
// Tooltip horizontal start: pad + max visual marker width + gap.
const TOOLTIP_LEFT = STRIP_PAD_LEFT + MARKER_WIDTH * 3 + 8

export default function QuickNav() {
  const t = useT()
  const messages = useStore((s) => s.messages)
  const [hoveredId, setHoveredId] = useState<string | null>(null)
  const [mouseY, setMouseY] = useState<number | null>(null)
  const [scales, setScales] = useState<Record<string, number>>({})
  const stripRef = useRef<HTMLDivElement>(null)

  const turns: ChatMessage[] = useMemo(
    () => messages.filter((m) => m.role === 'user'),
    [messages],
  )

  const getScale = useCallback(
    (centerY: number): number => {
      if (mouseY === null || !stripRef.current) return 1
      const rect = stripRef.current.getBoundingClientRect()
      const dist = Math.abs(mouseY - (rect.top + centerY))
      const maxRange = 50
      if (dist > maxRange) return 1
      const t = 1 - dist / maxRange
      return 1 + t * 2
    },
    [mouseY],
  )

  useLayoutEffect(() => {
    if (mouseY === null) { setScales({}); return }
    let running = true
    let raf = 0
    const tick = () => {
      if (!running) return
      const next: Record<string, number> = {}
      turns.forEach((t, i) => {
        next[t.id] = getScale(i * (MARKER_HEIGHT + MARKER_GAP) + MARKER_HEIGHT / 2)
      })
      setScales(next)
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => { running = false; cancelAnimationFrame(raf) }
  }, [mouseY, getScale, turns])

  if (turns.length < 2) return null

  const handleClick = (msgId: string) => {
    const el = document.querySelector(`[data-msg-id="${msgId}"]`)
    if (!el) return
    el.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const hoveredIdx = hoveredId !== null ? turns.findIndex((t) => t.id === hoveredId) : -1
  const hovered = hoveredIdx >= 0 ? turns[hoveredIdx] : undefined
  const tooltipTop = hoveredIdx >= 0 ? 8 + hoveredIdx * (MARKER_HEIGHT + MARKER_GAP) + MARKER_HEIGHT / 2 : 0

  return (
    <div className="absolute left-0 top-0 bottom-0 z-10 pointer-events-none flex items-center">
      {/* Marker strip */}
      <div
        ref={stripRef}
        className="pointer-events-auto relative"
        style={{ paddingLeft: STRIP_PAD_LEFT, paddingTop: 8, paddingBottom: 8 }}
        onMouseMove={(e) => setMouseY(e.clientY)}
        onMouseLeave={() => { setMouseY(null); setHoveredId(null) }}
      >
        <div className="flex flex-col items-start" style={{ gap: MARKER_GAP }}>
          {turns.map((t) => {
            const scale = scales[t.id] ?? 1
            const isHovered = hoveredId === t.id
            return (
              <button
                key={t.id}
                onClick={() => handleClick(t.id)}
                onMouseEnter={() => setHoveredId(t.id)}
                className="rounded-full cursor-pointer flex-shrink-0 origin-left"
                style={{
                  width: MARKER_WIDTH,
                  height: MARKER_HEIGHT,
                  transform: `scaleX(${scale}) scaleY(${Math.min(scale, 2.5)})`,
                  transition: mouseY === null ? 'transform 200ms ease-out' : 'none',
                  backgroundColor: isHovered
                    ? 'rgb(var(--accent))'
                    : 'rgb(var(--ink-faint) / 0.35)',
                }}
              />
            )
          })}
        </div>

        {/* Tooltip preview */}
        {hovered && (
          <div
            className="absolute pointer-events-none"
            style={{
              left: MARKER_WIDTH * 3 + 8,
              top: 8 + hoveredIdx * (MARKER_HEIGHT + MARKER_GAP) + MARKER_HEIGHT / 2,
              transform: 'translateY(-50%)',
            }}
          >
            <div className="bg-surface-raised border border-surface-border rounded-md shadow-md px-3 py-2" style={{ width: 'max-content', minWidth: 120, maxWidth: 320 }}>
              <p className="text-sm text-ink whitespace-pre-wrap break-words line-clamp-2">
                {hovered.content}
              </p>
              <div className="mt-1 text-[10px] text-ink-faint">
                {t('chat.turnOf', { current: hoveredIdx + 1, total: turns.length })}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
