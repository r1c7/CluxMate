// The desktop's half of the frozen `cluxmate worktree` contract, in one pure
// place: how to read the CLI's one line of JSON, how to describe a transport
// failure, and which sessions live in a linked worktree. Imported by the main
// process (src/main/worktree.ts) and by the renderer (the sidebar badge and the
// "移除工作树…" entry).
//
// No electron and no fs: the whole file is data logic, so desktop/tests can
// exercise it directly with the Node test runner.

// --- reading the CLI's output ------------------------------------------------

// §A: `--json` prints EXACTLY one line of JSON on stdout for both outcomes, and
// a failure additionally exits 1. A caller therefore reads the payload, not the
// exit code.
export type CluxmateOk = { ok: true; [key: string]: unknown }

export type CluxmateFailure = {
  ok: false
  error: string
  message: string
  details?: Record<string, unknown>
}

export type CluxmateJsonResult = CluxmateOk | CluxmateFailure

// A traceback or an interpreter error can be long; its useful line is the LAST
// one, and a dialog is the wrong place for kilobytes.
const STDERR_TAIL_CHARS = 500
const LINE_TAIL_CHARS = 200

// Parse the LAST non-empty stdout line. Anything that is not a JSON object is
// `bad-output` — a code that is NOT part of the CLI's closed §A set, because it
// describes the transport rather than what the CLI decided (`timeout` and
// `python-missing` are the other two, synthesized by src/main/worktree.ts).
export function parseCluxmateJson(stdout: string, stderr: string, code: number): CluxmateJsonResult {
  const line = lastNonEmptyLine(stdout)
  if (line !== null) {
    let value: unknown
    try {
      value = JSON.parse(line)
    } catch {
      value = undefined
    }
    // The one place the JSON boundary is crossed: §A is the frozen contract and
    // every payload carries `ok`, so the parsed object IS the result — an object
    // that somehow lacks the key is still returned as-is rather than being
    // re-judged here, because deciding "success" from anything but the payload
    // is exactly what the exit code must not do.
    if (isJsonObject(value)) return value as CluxmateJsonResult
  }
  return { ok: false, error: 'bad-output', message: badOutputMessage(stderr, line, code) }
}

// The spawn `error` event: ENOENT means either python is not on PATH or the
// requested cwd is gone (indistinguishable from here), EACCES a blocked
// interpreter. The client reports all of them under `python-missing`, so the
// message has to carry the detail that tells those cases apart.
export function spawnErrorMessage(err: unknown): string {
  const e = (err ?? {}) as { code?: unknown; message?: unknown }
  const detail = typeof e.message === 'string' && e.message ? e.message : String(err ?? 'unknown error')
  return e.code === 'ENOENT'
    ? `python is not on PATH, or the working directory is gone (${detail})`
    : detail
}

function isJsonObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function lastNonEmptyLine(stdout: string): string | null {
  const lines = stdout.split('\n')
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim()
    if (line) return line
  }
  return null
}

// Nothing readable came back. Prefer stderr (where a Python traceback or a
// missing-interpreter message lands), then the offending line itself, then the
// exit code — the message is the user's only clue either way.
function badOutputMessage(stderr: string, line: string | null, code: number): string {
  const err = tail(stderr.trim(), STDERR_TAIL_CHARS)
  if (err) return err
  if (line !== null) return `cluxmate printed no JSON object: ${tail(line, LINE_TAIL_CHARS)}`
  return `cluxmate printed nothing (exit code ${code})`
}

function tail(text: string, max: number): string {
  return text.length > max ? `…${text.slice(-max)}` : text
}

// --- the session shape the sidebar keys on -----------------------------------

// The two columns a worktree session carries (§C), read structurally so this
// module stays independent of shared/types.ts; a session from before those
// columns existed simply has neither field.
export interface WorktreeSessionFields {
  worktree_name?: string | null
  worktree_branch?: string | null
}

// The badge label: the worktree's name (`me`), or null when the session is not
// in a worktree. The branch is the badge's tooltip, so the caller reads it off
// the session directly.
export function worktreeBadgeLabel(session: WorktreeSessionFields | null | undefined): string | null {
  const name = typeof session?.worktree_name === 'string' ? session.worktree_name.trim() : ''
  return name || null
}

// `create` writes worktree_name and worktree_branch together, and both the badge
// and the "移除工作树…" entry key on the NAME: it is what `worktree remove` takes
// as its target, while a row without one is an ordinary session.
export function isWorktreeSession(session: WorktreeSessionFields | null | undefined): boolean {
  return worktreeBadgeLabel(session) !== null
}
