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
