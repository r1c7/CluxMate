// The desktop's client for the frozen `cluxmate worktree <action> --json` CLI
// (§A of the Phase 1B plan). Every worktree this app creates or removes goes
// through a TRANSIENT `python -m cluxmate worktree …` child: the naming rules,
// the `.worktrees/` container, the `info/exclude` write, the dirty-tree guard
// and the project-root resolution live in cluxmate/core/worktree.py and are
// shared with the CLI/TUI, so the main process never runs `git worktree` itself
// and never re-implements a rule.
//
// Invariants, all of them load-bearing:
//   * NEVER throws and never rejects. Callers branch on `ok`; a dead
//     interpreter, a timeout and unreadable output are results too
//     (`python-missing` / `timeout` / `bad-output` — transport codes that are
//     deliberately NOT part of the CLI's closed §A set).
//   * stdin is DETACHED (`stdio[0] = 'ignore'`): a child that inherits a pipe on
//     stdin is the documented Windows hang (see the AGENTS.md note on
//     `agent stdio`), and nothing here writes to it.
//   * every call is bounded, and the timeout kills the child instead of waiting
//     for it.
//   * one interpreter and one environment: `python-runtime.ts`, the same module
//     the long-lived agent bridge spawns with.

import { spawn, type ChildProcess } from 'child_process'
// The .ts extension is deliberate, as in src/main/skills.ts: it is what lets
// the Node test runner (or a smoke script) load this module directly, and
// tsconfig.main.json enables allowImportingTsExtensions for it. python-runtime
// is the only electron dependency, so a direct load needs an electron stub —
// nothing in desktop/tests does this today.
import { agentEnv, pythonCommand } from './python-runtime.ts'
import {
  parseCluxmateJson,
  spawnErrorMessage,
  type CluxmateFailure,
  type CluxmateJsonResult,
} from '../shared/worktree-rules.ts'

// `info` is a couple of git reads; `create` checks out a whole tree and
// `remove` deletes one, which is slow on a big repository (and slower still
// with an lfs smudge on checkout).
const INFO_TIMEOUT_MS = 10_000
const MUTATE_TIMEOUT_MS = 60_000

// --- §A result shapes --------------------------------------------------------
// Key for key, and §A's own note — "TS 侧按 key 解析，多余键无害" (the TS side
// parses by key; extra keys are harmless) — is why the wrappers build these
// from the parsed JSON rather than casting it.

export interface WorktreeInfoResult {
  ok: true
  /** The resolved session directory, as the CLI saw it. */
  cwd: string
  /** The main worktree — informative; NOT what a session's config is read from. */
  root: string
  /** The directory a session here reads its project config from (§E). */
  config_root: string
  is_worktree: boolean
  branch: string | null
}

export interface WorktreeCreateResult {
  ok: true
  name: string
  path: string
  branch: string
  base: string
  base_ref: string
  project_root: string
  repo_root: string
  /** Uncommitted files the main worktree had (only reachable with allowDirty). */
  dirty: string[]
}

export interface WorktreeRow {
  path: string
  /** Container-relative name, `''` for a tree this app did not create. */
  name: string
  branch: string
  head: string
  is_main: boolean
  is_current: boolean
}

export interface WorktreeListResult {
  ok: true
  worktrees: WorktreeRow[]
}

// Named `…Payload`, not `…Result`: §D gives `WorktreeRemoveResult` to the IPC
// reply in shared/types.ts (what ipc-handlers returns to the renderer), while
// this is the CLI's own payload — Task 5 maps the former onto the latter.
export interface WorktreeRemovePayload {
  ok: true
  path: string
  branch: string
  branch_deleted: boolean
  pruned: boolean
  /** Present only when the branch could not be deleted (not a failure). */
  message?: string
}

export type WorktreeResult<T> = T | CluxmateFailure

export interface WorktreeCreateOptions {
  /** Project directory the CLI resolves the repository from. */
  cwd: string
  /** Worktree name; blank ⇒ derived from `title`, else `wt`. */
  name?: string
  /** Session title the name is derived from. */
  title?: string
  /** Ref to base the tree on; blank ⇒ HEAD. */
  base?: string
  /** Branch name; blank ⇒ `cluxmate/<name>`. */
  branch?: string
  /**
   * Create even though the main worktree has uncommitted changes. The dialog
   * asks the user to commit or stash first, so it does not pass this today; it
   * is the CLI's own escape hatch (`--allow-dirty`) and is kept here so the
   * client covers the subcommand it wraps.
   */
  allowDirty?: boolean
}

export interface WorktreeRemoveOptions {
  cwd: string
  /** Worktree name, or a path under `<repo>/.worktrees`. */
  target: string
  /** Discard uncommitted changes in the tree. */
  force?: boolean
  /** Keep the branch after removing the tree (default: delete it). */
  keepBranch?: boolean
}

// --- the four actions --------------------------------------------------------

// Resolve a directory's project root. Fail-safe for callers: `ok:false` covers
// "not a git repository" as well as a missing interpreter, a timeout and
// unreadable output, so a caller that falls back to its own cwd on failure gets
// today's behaviour in every degraded case.
export async function worktreeInfo(cwd: string): Promise<WorktreeResult<WorktreeInfoResult>> {
  const parsed = await call(['info'], cwd, INFO_TIMEOUT_MS)
  if (!parsed.ok) return parsed
  const bad = requireStrings(parsed, ['cwd', 'root', 'config_root'])
  if (bad) return bad
  return {
    ok: true,
    cwd: text(parsed.cwd),
    root: text(parsed.root),
    config_root: text(parsed.config_root),
    is_worktree: parsed.is_worktree === true,
    branch: nullableText(parsed.branch),
  }
}

export async function worktreeCreate(
  opts: WorktreeCreateOptions,
): Promise<WorktreeResult<WorktreeCreateResult>> {
  const args = ['create']
  pushFlag(args, '--name', opts.name)
  pushFlag(args, '--title', opts.title)
  pushFlag(args, '--base', opts.base)
  pushFlag(args, '--branch', opts.branch)
  if (opts.allowDirty) args.push('--allow-dirty')

  const parsed = await call(args, opts.cwd, MUTATE_TIMEOUT_MS)
  if (!parsed.ok) return parsed
  const bad = requireStrings(parsed, ['name', 'path', 'branch', 'project_root'])
  if (bad) return bad
  return {
    ok: true,
    name: text(parsed.name),
    path: text(parsed.path),
    branch: text(parsed.branch),
    base: text(parsed.base),
    base_ref: text(parsed.base_ref),
    project_root: text(parsed.project_root),
    repo_root: text(parsed.repo_root),
    dirty: stringList(parsed.dirty),
  }
}

export async function worktreeList(cwd: string): Promise<WorktreeResult<WorktreeListResult>> {
  const parsed = await call(['list'], cwd, MUTATE_TIMEOUT_MS)
  if (!parsed.ok) return parsed
  if (!Array.isArray(parsed.worktrees)) {
    return badOutput('list reported success without a worktrees array')
  }
  return {
    ok: true,
    worktrees: parsed.worktrees.map((row) => {
      const r = (row ?? {}) as Record<string, unknown>
      return {
        path: text(r.path),
        name: text(r.name),
        branch: text(r.branch),
        head: text(r.head),
        is_main: r.is_main === true,
        is_current: r.is_current === true,
      }
    }),
  }
}

export async function worktreeRemove(
  opts: WorktreeRemoveOptions,
): Promise<WorktreeResult<WorktreeRemovePayload>> {
  const args = ['remove', opts.target]
  if (opts.force) args.push('--force')
  if (opts.keepBranch) args.push('--keep-branch')

  const parsed = await call(args, opts.cwd, MUTATE_TIMEOUT_MS)
  if (!parsed.ok) return parsed
  const bad = requireStrings(parsed, ['path'])
  if (bad) return bad
  const payload: WorktreeRemovePayload = {
    ok: true,
    path: text(parsed.path),
    branch: text(parsed.branch),
    branch_deleted: parsed.branch_deleted === true,
    pruned: parsed.pruned === true,
  }
  // §A's optional key: the tree is gone but its branch was kept, and the CLI
  // explains why. Dropping it would hide the reason from the user.
  if (typeof parsed.message === 'string' && parsed.message) payload.message = parsed.message
  return payload
}

// --- the transient child -----------------------------------------------------

type RunFailure = 'python-missing' | 'timeout'

interface RunResult {
  code: number
  stdout: string
  stderr: string
  /**
   * `null` when the child ran to completion, whatever its exit code. The
   * brief's `{code, stdout, stderr}` plus this one field: a timed-out or
   * unscorable run must be reported under its OWN transport code, and `code`
   * alone cannot say which of the two it was.
   */
  failure: RunFailure | null
}

async function call(args: string[], cwd: string, timeoutMs: number): Promise<CluxmateJsonResult> {
  const r = await runCluxmate(args, cwd, timeoutMs)
  if (r.failure === 'timeout') {
    return {
      ok: false,
      error: 'timeout',
      message: `cluxmate worktree ${args[0]} timed out after ${Math.round(timeoutMs / 1000)}s`,
    }
  }
  if (r.failure === 'python-missing') {
    return { ok: false, error: 'python-missing', message: r.stderr }
  }
  return parseCluxmateJson(r.stdout, r.stderr, r.code)
}

// Run one `cluxmate worktree … --json` to completion. Resolves — never rejects —
// for every outcome: a spawn error, a timeout, a non-zero exit.
function runCluxmate(args: string[], cwd: string, timeoutMs: number): Promise<RunResult> {
  return new Promise((resolve) => {
    let stdout = ''
    let stderr = ''
    let settled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    const settle = (result: RunResult): void => {
      if (settled) return
      settled = true
      if (timer) clearTimeout(timer)
      resolve(result)
    }

    let proc: ChildProcess
    try {
      proc = spawn(pythonCommand(), ['-m', 'cluxmate', 'worktree', ...args, '--json'], {
        cwd,
        env: agentEnv(),
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      })
    } catch (e) {
      // spawn() throws synchronously only on invalid arguments, but the caller
      // must see a result even then.
      settle({ code: -1, stdout: '', stderr: spawnErrorMessage(e), failure: 'python-missing' })
      return
    }

    timer = setTimeout(() => {
      // Resolve first, then kill: `git worktree add` may have children of its
      // own and killing python does not necessarily close its pipes, so nothing
      // here may wait for 'close' (the Python side learned this the hard way —
      // see project_root._reap).
      settle({ code: -1, stdout, stderr: 'timed out', failure: 'timeout' })
      try {
        proc.kill()
      } catch { /* already gone */ }
    }, timeoutMs)

    proc.stdout?.on('data', (chunk: Buffer) => { stdout += chunk.toString('utf8') })
    proc.stderr?.on('data', (chunk: Buffer) => { stderr += chunk.toString('utf8') })
    // ENOENT (no python on PATH, or a cwd that no longer exists) and friends.
    proc.on('error', (err) => {
      settle({ code: -1, stdout, stderr: spawnErrorMessage(err), failure: 'python-missing' })
    })
    // 'close', not 'exit': it fires after both pipes are drained, so the whole
    // JSON line is in hand before it is parsed.
    proc.on('close', (code) => {
      settle({ code: code ?? -1, stdout, stderr, failure: null })
    })
  })
}

// --- helpers -----------------------------------------------------------------

// A blank option is "not given". The CLI takes an explicit `--name ""` at its
// word (→ bad-name) while an omitted one derives from `--title`, so passing an
// empty dialog field through verbatim would change the outcome. Values are
// trimmed, which is what the CLI does with `--base`/`--branch` anyway.
function pushFlag(args: string[], flag: string, value?: string): void {
  const v = (value ?? '').trim()
  if (v) args.push(flag, v)
}

// An `ok: true` payload missing a string the caller is about to use — a path, a
// project root — is as unusable as no payload at all, and the callers' fail-safe
// branch is `ok: false`, so it is reported there instead of becoming an empty
// path in the session store.
function requireStrings(payload: Record<string, unknown>, keys: readonly string[]): CluxmateFailure | null {
  const missing = keys.filter((key) => typeof payload[key] !== 'string' || !payload[key])
  return missing.length ? badOutput(`reported success without ${missing.join(', ')}`) : null
}

function badOutput(message: string): CluxmateFailure {
  return { ok: false, error: 'bad-output', message: `cluxmate worktree ${message}` }
}

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function nullableText(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []
}
