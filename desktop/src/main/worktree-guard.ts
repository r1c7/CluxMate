// The occupancy guard behind 「移除工作树」: which OTHER sessions still live inside
// the tree that is about to be deleted, and the containment test that decides it.
//
// One tree per session is the CREATE path's construction — a worktree session's
// cwd is exactly `<repo>/.worktrees/<name>` — but nothing ENFORCES it: the session
// table has no uniqueness constraint on cwd, so "change working directory", the
// CLI's `--cwd`, or opening the tree directory as a project can each file a second
// session in a tree that already has one. Deleting that tree would take a live
// session's working directory out from under it (and, since `remove` is forced,
// anything uncommitted in it), so the remove handler asks here FIRST — before the
// bridge is killed and before any `cluxmate worktree remove` call.
//
// The test is a SESSION ROW question, not a live-bridge one: a session that is
// merely cold right now will still run in that tree on its next turn, and the
// bridge map is empty for it either way.
//
// Pure path/string logic (no electron imports) so desktop/tests can exercise it
// directly with the Node test runner — main/ipc-handlers.ts imports electron,
// which the zero-dependency runner cannot load. Same convention as ./cwd-key.ts.

import * as path from 'path'

// Is `candidate` `container` itself, or somewhere below it? Both sides are
// resolved first, so a trailing separator, a `.` segment, a mixed `/`-`\`
// spelling (Windows) or a `..` that folds back in are all the same directory.
//
// `path.relative` is what decides containment, and each escape it can return
// means "not below": `..` climbs one level, a `..<sep>` prefix climbs further,
// and a cross-drive/cross-root candidate yields an ABSOLUTE path (Windows gives
// `D:\x` for a `C:` container). A sibling that merely shares a leading string is
// outside too — `<repo>/.worktrees/me-old` is not inside `<repo>/.worktrees/me`
// — because the comparison is over whole path SEGMENTS, never raw prefixes.
//
// An empty path is inside nothing and contains nothing (and `path.resolve('')`
// would silently mean the main process's own cwd, a directory neither caller
// asked about).
export function isInsideOrEqual(container: string, candidate: string): boolean {
  if (!container || !candidate) return false
  const rel = path.relative(path.resolve(container), path.resolve(candidate))
  return rel === '' || (rel !== '..' && !rel.startsWith('..' + path.sep) && !path.isAbsolute(rel))
}

// The rows whose cwd sits in `tree`, minus the session being removed (by id) —
// the sessions that still OWN that tree, whatever their bridge is doing. The
// order is the caller's: it names the first blocker, so the message reads the
// same way the session list does.
export function sessionsInTree<T extends { id: string; cwd: string }>(
  tree: string,
  sessions: readonly T[],
  excludeId?: string,
): T[] {
  return sessions.filter((s) => s.id !== excludeId && isInsideOrEqual(tree, s.cwd))
}

// ── the occupancy of a DELETION SET ───────────────────────────────────────────
// Deleting a whole group/project asks a different question from deleting one
// session. The sessions being deleted cannot block each other — the user asked
// for all of them to go, and nothing enforces one session per tree, so two rows
// can be sitting in the same directory (the guard's header above) — while every
// session OUTSIDE that set still blocks the tree it lives in. Same containment
// test, same row order, same answer as `sessionsInTree` when the set holds one
// id (pinned by desktop/tests/worktree-guard.test.ts): that equivalence is what
// lets the single-session path and the bulk path share one rule.
export function sessionsInTreeExcluding<T extends { id: string; cwd: string }>(
  tree: string,
  sessions: readonly T[],
  excludingIds: ReadonlySet<string>,
): T[] {
  return sessions.filter((s) => !excludingIds.has(s.id) && isInsideOrEqual(tree, s.cwd))
}

// A session row as this module reads it: identity, where it runs, and the
// worktree it RECORDS (§C's column, read structurally like shared/worktree-rules
// does — a row from before the column existed simply has none).
export interface WorktreeRow {
  id: string
  cwd: string
  worktree_name?: string | null
}

// One tree a bulk delete would take. ONE entry per tree, never one per session:
// two rows can name or occupy the same directory.
export interface TreeRemovalCandidate<T> {
  // The tree's absolute path, as the caller's resolver computed it.
  target: string
  // The deleting session whose recorded name resolved to this tree — the first
  // one in row order. It carries the name/branch the UI shows and anchors the
  // CLI call (its project config root is the repository the tree lives in).
  owner: T
  // Every DELETING session still running inside the tree. Wider than the owners
  // on purpose: a stale row (its recorded name points elsewhere) and a plain row
  // filed here by "change working directory" are just as capable of holding the
  // directory open on Windows, and the caller has to kill their bridges before
  // the tree can go.
  occupants: T[]
  // Sessions inside the tree that are NOT being deleted — the ones that make the
  // removal a refusal. Same answer as `sessionsInTreeExcluding`.
  blockers: T[]
}

// A deleting row that records a worktree it no longer runs in: nothing to
// remove, nothing to block. The name travels so the caller can say WHICH tree
// was left alone.
export interface StaleWorktreeRow<T> {
  session: T
  name: string
}

// Phase 1 of a bulk worktree removal, in one pure function: which trees the
// deletion set would take, and which recorded names are already stale. Every
// fact here comes from session ROWS and the resolver — no bridge is killed, no
// CLI is called, nothing is deleted — so a caller can validate the whole batch
// before touching anything, and show the user exactly what it is about to do.
//
// `treePathOf` is the caller's resolver (`worktreePathForSession` in the main
// process: the recorded name resolved against the session's project root, and
// only while the session still runs inside it). Injecting it keeps this module
// free of imports the Node test runner cannot resolve, and keeps the §E
// project-root policy in the one place that owns it.
export function planTreeRemovals<T extends WorktreeRow>(
  sessions: readonly T[],
  deletingIds: ReadonlySet<string>,
  treePathOf: (session: T) => string | null,
): { removable: TreeRemovalCandidate<T>[]; stale: StaleWorktreeRow<T>[] } {
  const deleting = sessions.filter((s) => deletingIds.has(s.id))
  const removable: TreeRemovalCandidate<T>[] = []
  const stale: StaleWorktreeRow<T>[] = []
  for (const session of deleting) {
    // A blank name is no worktree — the same trim the sidebar badge applies
    // (shared/worktree-rules.ts): such a row has no tree of its own, so it is
    // neither a target nor stale.
    const name = (session.worktree_name || '').trim()
    if (!name) continue
    const target = treePathOf(session)
    if (!target) {
      // It records a tree it no longer runs in (the working-directory control
      // does not clear the column): the name is not proof, so nothing is
      // removed — and it does not block anyone else's tree either.
      stale.push({ session, name })
      continue
    }
    // Already planned by an earlier row that resolved to the same directory:
    // removing it twice would fail on a tree that is already gone and report a
    // failure that never happened.
    if (removable.some((c) => isSamePath(c.target, target))) continue
    removable.push({
      target,
      owner: session,
      occupants: deleting.filter((s) => isInsideOrEqual(target, s.cwd)),
      blockers: sessionsInTreeExcluding(target, sessions, deletingIds),
    })
  }
  return { removable, stale }
}

// Two spellings of one directory — a trailing separator, a mixed `/` vs `\`, a
// different case on Windows — are the SAME tree. `isInsideOrEqual` is this
// module's one containment test, so equality is "each contains the other"
// rather than a string comparison; a target is never empty (it comes from the
// caller's resolver), so the empty-path answer cannot leak in here.
function isSamePath(a: string, b: string): boolean {
  return isInsideOrEqual(a, b) && isInsideOrEqual(b, a)
}
