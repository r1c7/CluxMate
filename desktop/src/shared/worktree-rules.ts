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

// --- the directory a new PLAIN session starts in ------------------------------

// A session row, read structurally like the shapes above: only the two directory
// fields the rule below needs.
export interface SessionDirFields {
  cwd?: string | null
  project_root?: string | null
}

// Where a new plain session starts when it INHERITS a directory — the one the
// last send came from, or the one being viewed (the store's chain, and the only
// two paths that are implicit; an explicit "new session in this project" passes a
// project directory and never reaches this rule).
//
// Inheriting a linked worktree would be wrong on three counts: a tree is ONE
// session's isolation (`create` makes one per session, and §C's occupancy guard
// refuses to remove a tree another session occupies), a second session in it
// shares that tree's undo history, and it would silently run on the tree's branch
// without anyone asking for a worktree. Per §E a session's `project_root` differs
// from its `cwd` exactly when it lives in a linked worktree — everywhere else
// (a plain repo, a plain-repo subdirectory, a non-git dir) the two are the same
// directory — so a difference IS the signal, and the project root is where the
// new session belongs.
//
// `owner` is the CALLER's lookup of the session running in `inherited` (the store
// owns the cwd comparison rules); null means no live session claims that
// directory, and the inherited path is returned untouched — inheriting what the
// user last worked in stays the rule for everything that is not a worktree.
export function plainSessionCwd(inherited: string, owner: SessionDirFields | null | undefined): string {
  const root = typeof owner?.project_root === 'string' ? owner.project_root.trim() : ''
  const cwd = typeof owner?.cwd === 'string' ? owner.cwd.trim() : ''
  if (!root || !cwd || root === cwd) return inherited
  return root
}

// --- which branches the PROJECT may switch to --------------------------------

// One row of `cluxmate worktree list --json`, read structurally so this module
// stays independent of the CLI's full payload (same convention as
// WorktreeSessionFields above): only the two fields the filter needs.
export interface WorktreeBranchRow {
  branch?: string | null
  is_main?: boolean
}

// May the composer's branch pill SWITCH a branch here?
//
// Inside a linked worktree, no: the branch is the tree's identity — `create`
// pins it to `cluxmate/<slug>`, the sidebar badge and the removal confirmation
// read it, and a Phase-2 merge would merge it back — so a session must not
// switch it out from under those readers. Git agrees for the common case: a
// branch checked out in the main worktree cannot be checked out in a linked one,
// so the dropdown would mostly offer errors. Switching inside a worktree stays
// possible from a terminal and from the agent's own shell; that is deliberate
// and is not what the UI offers.
//
// `false` does NOT hide the pill: it is rendered DISABLED and names the tree
// (see branchPillLabel), so the user can still see where the session runs.
//
// `isWorktree` is git truth about the CURRENT directory (`cluxmate worktree
// info`), not a session column, so this covers ANY session living in a linked
// worktree — including a plain one pointed at a tree the user made by hand — and
// allows switching again the moment a worktree session is moved to the main tree.
//
// Fail-safe: an absent field keeps today's behaviour (switching allowed).
export function canSwitchBranches(
  git: { inRepo?: boolean; isWorktree?: boolean } | null | undefined,
): boolean {
  return git?.inRepo === true && git.isWorktree !== true
}

// What the pill shows. In the project tree: the current branch. Inside a linked
// worktree: the TREE's name (`me`), because that is the session's identity while
// the branch is not switchable — the branch itself stays in the sidebar badge's
// tooltip and in the removal confirmation. Null when there is nothing to name.
export function branchPillLabel(
  git: { currentBranch?: string | null; isWorktree?: boolean; worktreeName?: string | null } | null | undefined,
): string | null {
  const branch = typeof git?.currentBranch === 'string' ? git.currentBranch.trim() : ''
  if (git?.isWorktree !== true) return branch || null
  const name = typeof git?.worktreeName === 'string' ? git.worktreeName.trim() : ''
  // A tree whose name could not be read still shows something true about it.
  return name || branch || null
}

// May this project offer "new session in a git worktree"? Only a repository has
// trees to make, and the sidebar asks the main process per project (`isGitRepo`,
// one `rev-parse`).
//
// Fail-open on purpose: `undefined` is "not answered yet" — a project that IS a
// repository must not lose the entry to a slow probe, so an unknown verdict shows
// the entry (today's behaviour) and only a confirmed `false` hides it. The dialog
// stays the backstop: creating one against a non-repository still fails with the
// CLI's own `not-a-repo`.
export function canCreateWorktree(isRepo: boolean | null | undefined): boolean {
  return isRepo !== false
}

// Hide the branches a LINKED worktree currently has checked out.
//
// They cannot be switched to from the main tree (git answers "already checked
// out at …"), and they are the identity of another session's tree — pulling one
// into the project's tree would break the one-tree-one-branch rule the worktree
// columns, the removal path and the Phase-2 finish actions are built on.
//
// Only `is_main === false` rows count: the main worktree's own branch is the
// ordinary current branch and stays in the list, and a tree removed with
// `--keep-branch` puts its branch back in the list, which is exactly what
// keeping it is for (Phase 2's 「保留成分支」).
//
// Fail-safe by construction: no rows — an older CLI, a non-repository, a failed
// call — filter nothing, so the dropdown shows today's list.
export function withoutWorktreeBranches(
  branches: readonly string[],
  rows: readonly WorktreeBranchRow[] | null | undefined,
): string[] {
  const all = [...branches]
  const held = new Set<string>()
  for (const row of rows ?? []) {
    if (row?.is_main === true) continue
    const branch = typeof row?.branch === 'string' ? row.branch.trim() : ''
    if (branch) held.add(branch)
  }
  return held.size ? all.filter((branch) => !held.has(branch)) : all
}
