import React, { useMemo } from 'react'
import { useT } from '../useI18n'

interface Props {
  content: string
}

export default function DiffCard({ content }: Props) {
  const t = useT()
  const diffBlocks = useMemo(() => {
    const blocks: string[] = []
    const regex = /```diff\n([\s\S]*?)```/g
    let match
    while ((match = regex.exec(content)) !== null) {
      blocks.push(match[1])
    }
    return blocks
  }, [content])

  if (diffBlocks.length === 0) return null

  return (
    <div className="mt-3 space-y-2">
      {diffBlocks.map((block, i) => {
        const added = block.split('\n').filter(l => l.startsWith('+') && !l.startsWith('+++')).length
        const removed = block.split('\n').filter(l => l.startsWith('-') && !l.startsWith('---')).length
        return (
          <div key={i} className="bg-surface-raised rounded-lg overflow-hidden border border-surface-border">
            <div className="px-3 py-1 bg-sidebar text-xs text-ink-soft flex items-center gap-3">
              <span>{t('diff.codeChange')}</span>
              <span className="text-emerald-600">+{added}</span>
              <span className="text-red-600">-{removed}</span>
            </div>
            <pre className="p-3 text-xs overflow-x-auto">
              {block.split('\n').map((line, j) => {
                let color = 'text-ink-soft'
                let bg = ''
                if (line.startsWith('+') && !line.startsWith('+++')) {
                  color = 'text-emerald-700'; bg = 'bg-emerald-500/10'
                } else if (line.startsWith('-') && !line.startsWith('---')) {
                  color = 'text-red-700'; bg = 'bg-red-500/10'
                } else if (line.startsWith('@@')) {
                  color = 'text-blue-600'
                }
                return (
                  <div key={j} className={`${bg} ${color} leading-5`}>
                    {line}
                  </div>
                )
              })}
            </pre>
          </div>
        )
      })}
    </div>
  )
}
