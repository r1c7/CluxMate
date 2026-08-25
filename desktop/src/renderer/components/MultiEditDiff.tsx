import React, { useMemo, useState } from 'react'
import { diffLines } from 'diff'
import { useStore } from '../stores'
import { useT } from '../useI18n'
import { tGlobal } from '../i18n'

// Shared multi_edit diff rendering, used by both the pre-approval BatchEditCard
// and the post-approval ToolCallCard so the two read as one system.

export type Edit = { path: string; old_string: string; new_string: string }

// Display label for a tool name. The two edit tools (single-file
// search_replace and batch multi_edit) both surface as "edit" so the UI reads
// clearly and doesn't leak the internal tool names as distinct concepts.
// write_file stays "write" — its card uses the same diff form, but it's a whole
// -file write (often a new file), not a partial edit.
export function toolDisplayName(name: string): string {
  if (name === 'search_replace' || name === 'multi_edit') return tGlobal('edit.toolEdit')
  if (name === 'write_file' || name === 'multi_write') return tGlobal('edit.toolWrite')
  if (name === 'delete_file') return tGlobal('edit.toolDelete')
  return name
}

// Unique file paths across a set of edits, preserving first-seen order.
export function uniquePaths(edits: Edit[]): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const e of edits) {
    if (!seen.has(e.path)) { seen.add(e.path); out.push(e.path) }
  }
  return out
}

// Normalize a file-mutation tool's input into a uniform edit list so
// multi_edit, single-file search_replace, and write_file all share the same
// diff preview + result rendering. Returns null for tools that aren't file
// edits. write_file becomes an all-additions edit (old_string empty → the whole
// content shows as added), which is the natural diff for a newly written file.
export function editsFromToolInput(name: string, input: unknown): Edit[] | null {
  const i = (input || {}) as Record<string, unknown>
  if (name === 'multi_edit') {
    // Sanitize: the model's edits array is untrusted — an entry missing
    // old_string/new_string would make jsdiff's tokenizer crash on
    // undefined.split('\n') and take the whole renderer down (ErrorBoundary
    // "Render Error" on every open once saved into the display transcript).
    const edits = Array.isArray(i.edits) ? (i.edits as unknown[]) : []
    return edits.map((raw) => {
      const e = (raw || {}) as Record<string, unknown>
      return {
        path: typeof e.path === 'string' ? e.path : '',
        old_string: typeof e.old_string === 'string' ? e.old_string : '',
        new_string: typeof e.new_string === 'string' ? e.new_string : '',
      }
    })
  }
  if (name === 'search_replace') {
    const path = typeof i.path === 'string' ? i.path : (i.file_path as string) || ''
    if (typeof i.old_string !== 'string' || typeof i.new_string !== 'string') return null
    return [{ path, old_string: i.old_string, new_string: i.new_string }]
  }
  if (name === 'write_file') {
    const path = typeof i.path === 'string' ? i.path : (i.file_path as string) || ''
    if (typeof i.content !== 'string') return null
    return [{ path, old_string: '', new_string: i.content }]
  }
  if (name === 'multi_write') {
    // Batch write: each file is an all-additions edit, same as write_file.
    const files = Array.isArray(i.files) ? (i.files as Record<string, unknown>[]) : []
    return files.map((f) => ({
      path: typeof f.path === 'string' ? f.path : '',
      old_string: '',
      new_string: typeof f.content === 'string' ? f.content : '',
    }))
  }
  return null
}

type Row = { type: 'add' | 'del' | 'ctx'; text: string }

// Line-level diff of a single edit's old_string vs new_string. We only have the
// fragment (not the whole file), so there are no absolute line numbers — just
// +/−/context gutters, matching the app's hand-rolled diff style (DiffCard).
function diffRows(oldStr: string, newStr: string): Row[] {
  const rows: Row[] = []
  for (const part of diffLines(oldStr ?? '', newStr ?? '')) {
    const lines = part.value.split('\n')
    if (lines.length && lines[lines.length - 1] === '') lines.pop()
    const type: Row['type'] = part.added ? 'add' : part.removed ? 'del' : 'ctx'
    for (const text of lines) rows.push({ type, text })
  }
  return rows
}

export function basename(p: string): string {
  const parts = p.split(/[/\\]/)
  return parts[parts.length - 1] || p
}
export function dirname(p: string): string {
  const i = Math.max(p.lastIndexOf('/'), p.lastIndexOf('\\'))
  return i > 0 ? p.slice(0, i) : ''
}

// Render a file path relative to the session's working dir, so cards show
// `src/foo.ts` instead of `E:\workspace\proj\src\foo.ts`. Slashes are
// normalized to `/`. Falls back to the original (normalized) path when it's
// outside the working dir or the dir is unknown. Case-insensitive prefix match
// so Windows drive-letter casing doesn't defeat it.
export function relativePath(p: string, cwd: string): string {
  const norm = (s: string) => s.replace(/\\/g, '/').replace(/\/+$/, '')
  const np = norm(p)
  if (!cwd) return np
  const nc = norm(cwd)
  if (np.toLowerCase() === nc.toLowerCase()) return '.'
  const prefix = nc + '/'
  if (np.toLowerCase().startsWith(prefix.toLowerCase())) return np.slice(prefix.length)
  return np
}

// Per-file and total add/del counts from the actual line diff.
export function editStats(edits: Edit[]): { per: { add: number; del: number }[]; add: number; del: number } {
  let add = 0, del = 0
  const per = edits.map((e) => {
    const rows = diffRows(e.old_string, e.new_string)
    const a = rows.filter((r) => r.type === 'add').length
    const d = rows.filter((r) => r.type === 'del').length
    add += a; del += d
    return { add: a, del: d }
  })
  return { per, add, del }
}

function FileDiff({ edit }: { edit: Edit }) {
  const rows = useMemo(() => diffRows(edit.old_string, edit.new_string), [edit])
  return (
    <pre className="text-[11px] font-mono leading-5 overflow-x-auto max-h-72 overflow-y-auto bg-surface/60">
      {rows.map((r, j) => {
        const cls =
          r.type === 'add' ? 'bg-emerald-500/10 text-emerald-700' :
          r.type === 'del' ? 'bg-red-500/10 text-red-700' :
          'text-ink-faint'
        const glyph = r.type === 'add' ? '+' : r.type === 'del' ? '−' : ' '
        return (
          <div key={j} className={`${cls} px-2`}>
            <span className="select-none text-ink-faint/50 mr-2">{glyph}</span>
            {r.text || ' '}
          </div>
        )
      })}
    </pre>
  )
}

// Collapsible per-file list with inline fragment diffs (old → new). Each row
// toggles its own diff. `defaultOpenFirst` expands the first file (pre-approval
// review); leave false so the post-approval card stays collapsed until drilled.
export function MultiEditFileList({
  edits, defaultOpenFirst = false,
}: {
  edits: Edit[]
  defaultOpenFirst?: boolean
}) {
  const cwd = useStore((s) => s.workingDir)
  const stats = useMemo(() => editStats(edits), [edits])
  const [expanded, setExpanded] = useState<Set<number>>(
    () => new Set(defaultOpenFirst && edits.length > 0 ? [0] : [])
  )
  const toggle = (i: number) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })

  return (
    <div className="divide-y divide-surface-border/40">
      {edits.map((edit, i) => {
        const isOpen = expanded.has(i)
        const rel = relativePath(edit.path, cwd)
        const dir = dirname(rel)
        const s = stats.per[i] || { add: 0, del: 0 }
        return (
          <div key={i}>
            <button
              onClick={() => toggle(i)}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-left hover:bg-surface-raised/60 transition-colors"
              title={rel}
            >
              <span className={`text-ink-faint text-[10px] flex-shrink-0 transition-transform ${isOpen ? 'rotate-90' : ''}`}>▶</span>
              <span className="text-xs font-mono text-ink-soft truncate">{basename(rel)}</span>
              {dir && <span className="text-[10px] font-mono text-ink-faint/60 truncate min-w-0">{dir}</span>}
              <span className="ml-auto text-[10px] font-mono flex-shrink-0">
                {s.add > 0 && <span className="text-emerald-600">+{s.add}</span>}
                {s.del > 0 && <span className="text-red-600 ml-1">−{s.del}</span>}
              </span>
            </button>
            {isOpen && (
              <div className="px-3 pb-2">
                <div className="border border-surface-border rounded overflow-hidden">
                  <FileDiff edit={edit} />
                </div>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// The changed-files list shown under an applied edit. One clickable row per
// unique file — click opens a full-file diff in the right dock (content read
// from disk, before-state reconstructed by reversing the edits). Mirrors
// ChangedFilesCard's look; the active row is highlighted.
export function EditedFilesList({ edits, callId }: { edits: Edit[]; callId: string }) {
  const t = useT()
  const previewEditFiles = useStore((s) => s.previewEditFiles)
  const diffView = useStore((s) => s.diffView)
  const cwd = useStore((s) => s.workingDir)
  const paths = useMemo(() => uniquePaths(edits), [edits])
  const previewKey = `edit:${callId}`

  return (
    <div className="border-t border-surface-border">
      <div className="px-2.5 py-1 text-[10px] uppercase tracking-wide text-ink-faint/70">
        {t('edit.modifiedFiles')}
      </div>
      <div className="divide-y divide-surface-border/40">
        {paths.map((p) => {
          const rel = relativePath(p, cwd)
          const dir = dirname(rel)
          const isActive = diffView?.checkpointId === previewKey && diffView.activePath === p
          return (
            <button
              key={p}
              onClick={() => previewEditFiles(previewKey, edits, p)}
              className={`w-full flex items-center gap-2 px-3 py-1.5 text-left transition-colors ${
                isActive ? 'bg-accent/15' : 'hover:bg-surface-raised/60'
              }`}
              title={isActive ? t('edit.closePreview', { path: rel }) : t('edit.preview', { path: rel })}
            >
              <span className="text-ink-faint text-[11px] flex-shrink-0">⧉</span>
              <span className="text-xs font-mono text-ink-soft truncate">{basename(rel)}</span>
              {dir && <span className="text-[10px] font-mono text-ink-faint/60 truncate min-w-0">{dir}</span>}
            </button>
          )
        })}
      </div>
    </div>
  )
}
