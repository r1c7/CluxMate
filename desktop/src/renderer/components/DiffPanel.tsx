import React, { useMemo } from 'react'
import ReactDiffViewer, { DiffMethod } from 'react-diff-viewer-continued'
import type { CheckpointFileDiff } from '../../shared/types'
import { useStore } from '../stores'
import { isDarkTheme } from '../themes'
import { useT } from '../useI18n'

// react-diff-viewer-continued takes raw color strings (it renders plain divs,
// not Tailwind classes), so its palette can't reuse the `var(--…)` tokens
// directly. Read the resolved CSS variables off <html> at render time and build
// the styles object from them — this keeps the diff in sync with whatever theme
// is active (including the added/removed green/red, which swap to dark-friendly
// variants under dark themes).
function cssVar(name: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return v ? `rgb(${v})` : 'transparent'
}

function buildDiffStyles() {
  return {
    variables: {
      light: {
        diffViewerBackground: cssVar('--surface'),
        diffViewerColor: cssVar('--ink'),
        addedBackground: '#e7f3ea',
        addedColor: '#1a7f37',
        removedBackground: '#f9eae7',
        removedColor: '#b3392b',
        wordAddedBackground: '#c8e7d3',
        wordRemovedBackground: '#f3cbc3',
        gutterBackground: cssVar('--sidebar'),
        gutterColor: cssVar('--ink-faint'),
        codeFoldBackground: cssVar('--surface-raised'),
        emptyLineBackground: cssVar('--surface-raised'),
      },
      dark: {
        diffViewerBackground: cssVar('--surface'),
        diffViewerColor: cssVar('--ink'),
        addedBackground: 'rgb(63 185 80 / 0.16)',
        addedColor: 'rgb(126 231 160)',
        removedBackground: 'rgb(248 81 73 / 0.16)',
        removedColor: 'rgb(255 161 152)',
        wordAddedBackground: 'rgb(63 185 80 / 0.32)',
        wordRemovedBackground: 'rgb(248 81 73 / 0.32)',
        gutterBackground: cssVar('--sidebar'),
        gutterColor: cssVar('--ink-faint'),
        codeFoldBackground: cssVar('--surface-raised'),
        emptyLineBackground: cssVar('--surface-raised'),
      },
    },
    contentText: { fontFamily: 'monospace', fontSize: '11px' },
    // Tighten the two line-number columns (before / after) so the gap between
    // them is small — default padding leaves a wide gutter in unified view.
    gutter: {
      fontFamily: 'monospace',
      fontSize: '10px',
      minWidth: '20px',
      padding: '0 4px',
    },
    lineNumber: { padding: '0 2px' },
    marker: { padding: '0 4px', minWidth: 'auto' },
  }
}

const STATUS_LABEL_KEY: Record<CheckpointFileDiff['status'], string> = {
  A: 'diff.added', M: 'diff.modified', D: 'diff.deleted',
}
const STATUS_COLOR: Record<CheckpointFileDiff['status'], string> = {
  A: 'text-emerald-600', M: 'text-accent', D: 'text-red-600',
}

// Right-dock diff preview. Shares the dock with the agent inspector / checkpoint
// timeline; opened via store.openDiff (from the inline changed-files card or the
// timeline). Unified view (not split) since the dock is narrow.
export default function DiffPanel() {
  const t = useT()
  const diffView = useStore((s) => s.diffView)
  const onClose = useStore((s) => s.closeDiff)
  const setDiffActive = useStore((s) => s.setDiffActive)
  const theme = useStore((s) => s.theme)

  const dark = useMemo(() => isDarkTheme(theme), [theme])
  const styles = useMemo(() => buildDiffStyles(), [theme])

  if (!diffView) return null
  const { files, label } = diffView
  // Active file is derived from the store (single source of truth), so the
  // panel and the inline card highlight never desync. Falls back to the first.
  const active = diffView.activePath
    ? Math.max(0, files.findIndex((f) => f.path === diffView.activePath))
    : 0
  const file = files[active]

  return (
    <div className="h-full flex flex-col bg-surface">
      <div className="flex items-center gap-2 px-3 h-9 border-b border-surface-border flex-shrink-0">
        <span className="text-xs font-semibold text-ink-soft truncate">{t('diff.title', { label })}</span>
        <button
          onClick={onClose}
          className="text-ink-faint hover:text-ink text-base px-1 ml-auto transition-colors"
          title={t('common.close')}
        >×</button>
      </div>

      {files.length === 0 ? (
        <div className="flex-1 flex items-center justify-center text-ink-faint text-sm">
          {t('diff.noChanges')}
        </div>
      ) : (
        <div className="flex-1 flex flex-col min-h-0">
          {/* File list — horizontal-scrolling row of chips so the diff gets the
              vertical space. */}
          <div className="flex-shrink-0 border-b border-surface-border overflow-x-auto">
            <div className="flex gap-1 px-2 py-1.5 min-w-max">
              {files.map((f, i) => (
                <button
                  key={f.path}
                  onClick={() => setDiffActive(f.path)}
                  className={`flex items-center gap-1.5 px-2 py-1 rounded text-left flex-shrink-0 ${
                    i === active ? 'bg-surface-raised' : 'hover:bg-surface-raised/50'
                  }`}
                  title={f.path}
                >
                  <span className={`text-[10px] font-mono ${STATUS_COLOR[f.status]}`}>{f.status}</span>
                  <span className="text-xs font-mono text-ink-soft">{f.path.split('/').pop()}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Diff view */}
          <div className="flex-1 overflow-auto">
            {file && (
              <>
                <div className="px-3 py-1.5 border-b border-surface-border sticky top-0 bg-surface z-10">
                  <span className="text-[11px] font-mono text-ink-soft break-all">{file.path}</span>
                  <span className={`text-[10px] ml-2 ${STATUS_COLOR[file.status]}`}>
                    {t(STATUS_LABEL_KEY[file.status])}
                  </span>
                </div>
                <ReactDiffViewer
                  oldValue={file.old_content}
                  newValue={file.new_content}
                  splitView={false}
                  useDarkTheme={dark}
                  compareMethod={DiffMethod.WORDS}
                  styles={styles}
                />
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
