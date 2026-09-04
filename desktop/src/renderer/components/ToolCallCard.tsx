import React, { useState } from 'react'
import type { ToolCallEntry } from '../../shared/types'
import { useStore } from '../stores'
import { MultiEditFileList, EditedFilesList, editStats, editsFromToolInput, toolDisplayName, relativePath, type Edit } from './MultiEditDiff'
import { useT } from '../useI18n'
import { tGlobal } from '../i18n'

// One-line summary of a tool call for the collapsed header, e.g. "Read(path)",
// "Bash(cmd)". File-path tools show a path relative to the session working dir
// instead of an absolute one.
function summarize(tc: ToolCallEntry, cwd: string): string {
  const i = tc.input || {}
  const s = (v: unknown) => (typeof v === 'string' ? v : v == null ? '' : JSON.stringify(v))
  const rel = (v: unknown) => (typeof v === 'string' && v ? relativePath(v, cwd) : s(v))
  switch (tc.name) {
    case 'bash': return s(i.command)
    case 'read_file': return rel(i.path ?? i.file_path)
    case 'list_dir': return rel(i.path ?? i.dir_path ?? '.')
    case 'grep': return s(i.pattern)
    case 'search_replace': return rel(i.path ?? i.file_path)
    case 'write_file': return rel(i.path ?? i.file_path)
    case 'delete_file': return rel(i.path ?? i.file_path)
    case 'task': return s(i.subagent_type ?? i.description)
    case 'ask_user_question': {
      const qs = Array.isArray(i.questions) ? (i.questions as Record<string, unknown>[]) : []
      return qs.length === 1 ? s((qs[0] as Record<string, unknown>)?.question) : tGlobal('toolCard.questions', { count: qs.length, plural: qs.length === 1 ? '' : 's' })
    }
    case 'web_search': return s(i.query)
    case 'web_fetch': return s(i.url)
    case 'multi_edit': {
      const edits = Array.isArray(i.edits) ? (i.edits as Edit[]) : []
      return tGlobal('toolCard.files', { count: edits.length, plural: edits.length === 1 ? '' : 's' })
    }
    case 'multi_write': {
      const files = Array.isArray(i.files) ? i.files : []
      return tGlobal('toolCard.files', { count: files.length, plural: files.length === 1 ? '' : 's' })
    }
    default: {
      const first = Object.values(i)[0]
      return first != null ? s(first) : ''
    }
  }
}

const STATUS_DOT: Record<ToolCallEntry['status'], string> = {
  pending: 'bg-yellow-400 animate-pulse',
  running: 'bg-accent animate-pulse',
  done: 'bg-emerald-500',
  denied: 'bg-red-500',
  error: 'bg-red-500',
}

// Status → i18n key (pending maps to "awaiting approval").
const STATUS_LABEL_KEY: Record<ToolCallEntry['status'], string> = {
  pending: 'toolCard.status.awaiting',
  running: 'toolCard.status.running',
  done: 'toolCard.status.done',
  denied: 'toolCard.status.denied',
  error: 'toolCard.status.error',
}

function ToolCallCardInner({ tc }: { tc: ToolCallEntry }) {
  const t = useT()
  const [open, setOpen] = useState(false) // collapsed by default
  const cwd = useStore((s) => s.workingDir)

  // Edit tools (multi_edit + single-file search_replace) render as a per-file
  // diff preview (collapsed, with change counts) instead of dumping raw
  // path/old_string/new_string JSON.
  const multiEdits: Edit[] | null = editsFromToolInput(tc.name, tc.input)
  // After the edit is applied, list the changed files as clickable rows that
  // open a full-file diff in the right dock (reconstructed from disk).
  const showFiles = !!multiEdits && tc.status === 'done'

  const summary = summarize(tc, cwd)
  const hasBody = !!tc.result || Object.keys(tc.input || {}).length > 0
  const stats = multiEdits ? editStats(multiEdits) : null

  return (
    <div className="rounded-md border border-surface-border bg-surface-raised/60 overflow-hidden">
      <button
        onClick={() => hasBody && setOpen((v) => !v)}
        className={`w-full flex items-center gap-2 px-2.5 py-1.5 text-left ${hasBody ? 'cursor-pointer hover:bg-surface-raised' : 'cursor-default'}`}
      >
        <span className={`inline-block w-1.5 h-1.5 rounded-full flex-shrink-0 ${STATUS_DOT[tc.status]}`} />
        <span className="text-xs font-mono font-semibold text-ink-soft flex-shrink-0">{toolDisplayName(tc.name)}</span>
        {summary && (
          <span className="text-xs font-mono text-ink-faint truncate flex-1 min-w-0">{summary}</span>
        )}
        {stats && (
          <span className="text-[10px] font-mono flex-shrink-0">
            <span className="text-emerald-600">+{stats.add}</span>
            <span className="text-red-600 ml-1">−{stats.del}</span>
          </span>
        )}
        <span className="text-[10px] text-ink-faint/80 flex-shrink-0 ml-auto">{t(STATUS_LABEL_KEY[tc.status])}</span>
        {hasBody && (
          <span className={`text-ink-faint text-[10px] flex-shrink-0 transition-transform ${open ? 'rotate-90' : ''}`}>▶</span>
        )}
      </button>

      {open && hasBody && (
        multiEdits ? (
          <div className="border-t border-surface-border">
            {/* Inline fragment diff of each edit (old → new). */}
            <MultiEditFileList edits={multiEdits} />
            {/* After applying: clickable file list → full-file preview in the
                right dock. Errors keep showing the raw result instead. */}
            {showFiles ? (
              <EditedFilesList edits={multiEdits} callId={tc.call_id} />
            ) : tc.result ? (
              <pre className={`text-[11px] font-mono overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap break-words px-2.5 py-2 border-t border-surface-border ${tc.status === 'error' ? 'text-red-600' : 'text-ink-soft'}`}>
                {tc.result}
              </pre>
            ) : null}
          </div>
        ) : (
          <div className="border-t border-surface-border px-2.5 py-2 space-y-2">
            {/* delete_file's only input is the path, already shown in the
                header summary — dumping {"path": ...} JSON adds nothing. Other
                tools still show their full input for transparency. */}
            {tc.name !== 'delete_file' && Object.keys(tc.input || {}).length > 0 && (
              <pre className="text-[11px] font-mono text-ink-faint overflow-x-auto whitespace-pre-wrap break-words">
                {JSON.stringify(tc.input, null, 2)}
              </pre>
            )}
            {tc.result && (
              <pre className={`text-[11px] font-mono overflow-x-auto max-h-64 overflow-y-auto whitespace-pre-wrap break-words ${tc.status === 'error' ? 'text-red-600' : 'text-ink-soft'}`}>
                {tc.result}
              </pre>
            )}
          </div>
        )
      )}
    </div>
  )
}

// Memoized: during a streaming reply the live message re-renders on every text
// delta, which would otherwise re-render every tool card in it too. The store
// patches tool blocks immutably (only the changed tool gets a new `tc` ref), so
// default shallow comparison keeps settled cards static.
const ToolCallCard = React.memo(ToolCallCardInner)
export default ToolCallCard
