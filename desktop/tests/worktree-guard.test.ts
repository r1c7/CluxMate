// Unit tests for the main process's worktree occupancy guard — the pure half of
// how 「移除工作树」 decides whether some OTHER session still lives inside the tree
// it is about to delete. Run from desktop/: npm test
//
// Node's built-in runner executes .ts directly (type stripping). The import
// carries the .ts extension because Node resolves it itself, which is exactly
// why the module under test must not import electron: the handler that consumes
// it (main/ipc-handlers.ts) cannot be loaded here, main/cwd-key.ts's header
// documents the same convention.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as path from 'node:path'

import {
  isInsideOrEqual,
  planTreeRemovals,
  sessionsInTree,
  sessionsInTreeExcluding,
} from '../src/main/worktree-guard.ts'

// Synthetic, platform-native absolute paths: the guard is pure path/string logic
// and never touches the filesystem, so a real tree on disk would only add a
// temp-dir dependency (and a cleanup failure mode) to the test.
const REPO = path.resolve('cluxmate-guard-fixture', 'repo')
const TREE = path.join(REPO, '.worktrees', 'me')
const OTHER_TREE = path.join(REPO, '.worktrees', 'other')

const row = (id: string, cwd: string, title = id) => ({ id, cwd, title })
// A row that RECORDS a worktree (§C's column) — the shape the removal plan reads.
const wt = (id: string, cwd: string, worktree_name: string, title = id) =>
  ({ id, cwd, title, worktree_name })

// The main process's own resolver, reproduced faithfully: the recorded name
// resolves to `<project root>/.worktrees/<name>`, and ONLY while the session
// still runs inside that directory (ipc-handlers.ts's `worktreePathForSession`).
// The plan takes it as a callback precisely so this module stays filesystem-free.
const treePathOf = (s: { cwd: string; worktree_name?: string | null }): string | null => {
  const name = s.worktree_name || ''
  if (!name) return null
  const tree = path.join(REPO, '.worktrees', name)
  return isInsideOrEqual(tree, s.cwd) ? tree : null
}

// `planTreeRemovals` over a deletion set given as ids.
const plan = (rows: ReturnType<typeof wt>[], deleting: string[]) =>
  planTreeRemovals(rows, new Set(deleting), treePathOf)

test('the tree itself and anything below it are inside it', () => {
  assert.equal(isInsideOrEqual(TREE, TREE), true)
  assert.equal(isInsideOrEqual(TREE, path.join(TREE, 'sub')), true)
  assert.equal(isInsideOrEqual(TREE, path.join(TREE, 'a', 'b', 'c')), true)
})

test('a `.` segment or a trailing separator is the same directory', () => {
  assert.equal(isInsideOrEqual(TREE, TREE + path.sep), true)
  assert.equal(isInsideOrEqual(TREE + path.sep, TREE), true)
  assert.equal(isInsideOrEqual(TREE, path.join(TREE, '.')), true)
  assert.equal(isInsideOrEqual(TREE, path.join(TREE, '..', path.basename(TREE))), true)
})

test('escaping the tree with `..` is outside it', () => {
  assert.equal(isInsideOrEqual(TREE, path.join(TREE, '..')), false)
  assert.equal(isInsideOrEqual(TREE, path.join(TREE, '..', 'other')), false)
  // The main repository the trees are cut from sits one level up and out.
  assert.equal(isInsideOrEqual(TREE, REPO), false)
})

test('a name that merely starts with `..` is an ordinary child of the tree', () => {
  // Only a WHOLE `..` segment climbs out: `path.relative` answers `..hidden` for
  // <tree>/..hidden — a child directory whose name begins with two dots, not an
  // escape. The clause is therefore `!rel.startsWith('..' + path.sep)`, and this
  // is the only case in the file that tells it apart from the looser (wrong)
  // `!rel.startsWith('..')`, which passes every other assertion here.
  assert.equal(isInsideOrEqual(TREE, path.join(TREE, '..hidden')), true)
})

test('a sibling tree is outside, and so is a longer name that merely shares a prefix', () => {
  assert.equal(isInsideOrEqual(TREE, path.join(REPO, '.worktrees', 'other')), false)
  // `<repo>/.worktrees/me-old/sub` starts with `<repo>/.worktrees/me` as a
  // STRING; only a whole-segment comparison may call it outside.
  assert.equal(isInsideOrEqual(TREE, path.join(REPO, '.worktrees', 'me-old', 'sub')), false)
})

test('`/` and `\\` spellings of one tree agree', () => {
  // A path built with forward slashes (what the renderer hands over after
  // path.join-ing a user-typed folder) must land in the same tree as the native
  // one. On Windows both separators are real; on POSIX a backslash is an
  // ordinary character, so `forward` IS `TREE` and every assertion below would be
  // tautological — hence the whole test is Windows-only, which is also the
  // platform this guard ships on.
  if (path.sep !== '\\') return
  const forward = TREE.replace(/\\/g, '/')
  assert.equal(isInsideOrEqual(TREE, forward), true)
  assert.equal(isInsideOrEqual(forward, path.join(TREE, 'sub')), true)
  assert.equal(isInsideOrEqual(forward, path.join(forward, 'sub', 'deep')), true)
  // `<repo>\.worktrees\me` vs a candidate spelled with the other separator.
  const child = path.join(TREE, 'sub')
  assert.equal(isInsideOrEqual(TREE, child.replace(/\\/g, '/')), true)
  assert.equal(isInsideOrEqual(TREE.replace(/\\/g, '/'), child), true)
})

test("case differences follow this platform's path.relative semantics", () => {
  // path.win32 lowercases both sides before comparing, so a differently-cased
  // spelling is the SAME directory on Windows; path.posix is byte-exact. Assert
  // the answer this platform actually gives instead of inventing a rule.
  const expected = process.platform === 'win32'
  assert.equal(isInsideOrEqual(TREE.toUpperCase(), path.join(TREE, 'sub').toLowerCase()), expected)
  assert.equal(isInsideOrEqual(TREE, TREE.toUpperCase()), expected)
})

test('an empty path is inside nothing and contains nothing', () => {
  assert.equal(isInsideOrEqual('', TREE), false)
  assert.equal(isInsideOrEqual(TREE, ''), false)
  assert.equal(isInsideOrEqual('', ''), false)
})

test('no other session in the tree ⇒ nothing blocks the removal', () => {
  assert.deepEqual(sessionsInTree(TREE, []), [])
  assert.deepEqual(sessionsInTree(TREE, [row('a', REPO), row('b', path.join(REPO, '.worktrees', 'other'))]), [])
})

test('a session sitting exactly on the tree blocks it', () => {
  const me = row('s1', TREE)
  assert.deepEqual(sessionsInTree(TREE, [row('x', REPO), me]), [me])
})

test('a session deep inside the tree blocks it', () => {
  const me = row('s1', path.join(TREE, 'cluxmate', 'core'))
  assert.deepEqual(sessionsInTree(TREE, [me]), [me])
})

test('the session being removed is not its own blocker', () => {
  const self = row('s1', path.join(TREE, 'sub'))
  assert.deepEqual(sessionsInTree(TREE, [self], 's1'), [])
  // …even when it is not the only row, and regardless of where it sits.
  const other = row('s2', TREE)
  assert.deepEqual(sessionsInTree(TREE, [self, other], 's1'), [other])
})

test('every blocker is returned, in the order the rows came in', () => {
  // Deliberately not sorted by id or by depth: the caller's message names the
  // FIRST blocker, so the order has to be the session list's own.
  const rows = [
    row('s9', TREE),
    row('s1', path.join(TREE, 'deep', 'er')),
    row('s5', REPO),
    row('s3', path.join(TREE, 'sub')),
  ]
  assert.deepEqual(sessionsInTree(TREE, rows, 's9').map((s) => s.id), ['s1', 's3'])
})

// ── the occupancy of a DELETION SET ───────────────────────────────────────────
// Deleting a whole group/project asks a different question from deleting one
// session: the sessions being deleted cannot block each other, because they all
// go together. Everything OUTSIDE that set still blocks its tree.

test('a deletion set does not block itself', () => {
  // Two sessions the user filed into ONE tree (nothing enforces one tree per
  // session): both are about to be deleted, so neither is a blocker.
  const a = row('s1', TREE)
  const b = row('s2', path.join(TREE, 'sub'))
  assert.deepEqual(sessionsInTreeExcluding(TREE, [a, b], new Set(['s1', 's2'])), [])
})

test('a session outside the deletion set still blocks the tree', () => {
  const mine = row('s1', TREE)
  const outside = row('s9', path.join(TREE, 'sub'))
  const elsewhere = row('s8', REPO)
  // Row order preserved; the outside row from ANOTHER tree is not a blocker of
  // this one (the round below is the same tree, so it is).
  const rows = [mine, outside, elsewhere]
  assert.deepEqual(sessionsInTreeExcluding(TREE, rows, new Set(['s1'])), [outside])
  assert.deepEqual(sessionsInTreeExcluding(TREE, rows, new Set(['s1', 's9'])), [])
})

test('an empty deletion set blocks with every session in the tree', () => {
  const rows = [row('s1', TREE), row('s2', path.join(TREE, 'sub')), row('s3', REPO)]
  assert.deepEqual(sessionsInTreeExcluding(TREE, rows, new Set()), [rows[0], rows[1]])
})

test('a single excluded id is exactly what `sessionsInTree` answers', () => {
  // The per-session removal path keeps using `sessionsInTree(target, all, sid)`.
  // The bulk path answers "who is left once the deletion set is gone", and for a
  // one-session set the two MUST agree — that equivalence is what lets the two
  // paths share one occupancy rule without changing either answer.
  const rows = [
    row('s1', TREE),
    row('s2', path.join(TREE, 'sub')),
    row('s3', REPO),
    row('s4', TREE),
  ]
  for (const id of ['s1', 's2', 's3', 's4', 'nobody']) {
    assert.deepEqual(sessionsInTreeExcluding(TREE, rows, new Set([id])), sessionsInTree(TREE, rows, id))
  }
})

// ── the plan a bulk delete executes ───────────────────────────────────────────
// Phase 1 of `GROUP_DELETE {worktrees:'remove'}` is entirely this function: for
// the sessions being deleted, which trees go, which recorded names are stale,
// which bridges have to be killed first, and who (from outside the set) blocks.

test('a deleting session that still runs in its tree is one removal target', () => {
  const a = wt('s1', TREE, 'me')
  const p = plan([a, row('s2', REPO)], ['s1'])
  assert.deepEqual(p.removable.map((c) => c.target), [TREE])
  assert.deepEqual(p.removable[0].owner.id, 's1')
  // Both the owner and every other deleting session dwelling in the tree (there
  // are none here) are the bridges that must die before the directory can go.
  assert.deepEqual(p.removable[0].occupants.map((o) => o.id), ['s1'])
  assert.deepEqual(p.removable[0].blockers, [])
  assert.deepEqual(p.stale, [])
})

test('a session that left its tree is stale: not removed, and not a blocker', () => {
  // s1 still records "me" but was moved into the main tree; s2 really is in it.
  // The stale row neither contributes a target nor blocks s2's.
  const left = wt('s1', REPO, 'me')
  const inside = wt('s2', TREE, 'me')
  const p = plan([left, inside], ['s1', 's2'])
  assert.deepEqual(p.stale.map((s) => [s.session.id, s.name]), [['s1', 'me']])
  assert.deepEqual(p.removable.map((c) => c.owner.id), ['s2'])
  assert.deepEqual(p.removable[0].blockers, [])
})

test('a stale row sitting in ANOTHER deleting session’s tree is only an occupant', () => {
  // s1 records "other" (stale) but actually runs inside "me", which s2 owns and
  // both are losing: s1 adds no second target, must not be reported as stale
  // twice, and its bridge is still one of the ones locking "me".
  const s1 = wt('s1', TREE, 'other')
  const s2 = wt('s2', TREE, 'me')
  const p = plan([s1, s2], ['s1', 's2'])
  assert.deepEqual(p.stale.map((s) => s.session.id), ['s1'])
  assert.deepEqual(p.removable.map((c) => c.target), [TREE])
  assert.deepEqual(p.removable[0].occupants.map((o) => o.id), ['s1', 's2'])
  assert.deepEqual(p.removable[0].blockers, [])
})

test('a session with no worktree at all is neither a target nor stale', () => {
  const plain = row('s1', REPO)
  assert.deepEqual(plan([plain], ['s1']), { removable: [], stale: [] })
})

test('a blank worktree name is no worktree at all', () => {
  // The same rule the sidebar badge uses (worktree-rules.ts trims): a row whose
  // name is empty/whitespace is an ordinary session, not a stale worktree row.
  assert.deepEqual(plan([wt('s1', REPO, '   ')], ['s1']), { removable: [], stale: [] })
  assert.deepEqual(plan([wt('s1', REPO, '')], ['s1']), { removable: [], stale: [] })
})

test('two deleting sessions naming one tree collapse to ONE target', () => {
  // Removing the same directory twice would make the second call fail on a tree
  // that is already gone and report a failure that never happened. The first row
  // in list order owns the target; both rows are occupants (both bridges die).
  const a = wt('s1', TREE, 'me', 'first')
  const b = wt('s2', TREE, 'me', 'second')
  const p = plan([a, b], ['s1', 's2'])
  assert.equal(p.removable.length, 1)
  assert.equal(p.removable[0].owner.id, 's1')
  assert.deepEqual(p.removable[0].occupants.map((o) => o.id), ['s1', 's2'])
})

test('a session outside the deletion set inside the tree is its blocker', () => {
  const mine = wt('s1', TREE, 'me')
  const theirs = row('s2', path.join(TREE, 'deep', 'er'))
  const p = plan([mine, theirs], ['s1'])
  assert.deepEqual(p.removable[0].blockers.map((b) => b.id), ['s2'])
})

test('a deleting session in a different tree blocks nothing here', () => {
  const mine = wt('s1', TREE, 'me')
  const other = wt('s2', OTHER_TREE, 'other')
  const p = plan([mine, other], ['s1'])
  assert.deepEqual(p.removable.map((c) => c.target), [TREE])
  assert.deepEqual(p.removable[0].blockers, [])
  assert.deepEqual(p.stale, [])
})

test('targets and stale rows keep the session list’s order', () => {
  const rows = [
    wt('s1', TREE, 'me'),
    wt('s2', OTHER_TREE, 'other'),
    wt('s3', REPO, 'gone'),
  ]
  const p = plan(rows, ['s3', 's1', 's2'])
  // The order is the ROWS' order (the sidebar's), not the deletion set's.
  assert.deepEqual(p.removable.map((c) => c.target), [TREE, OTHER_TREE])
  assert.deepEqual(p.stale.map((s) => s.session.id), ['s3'])
})

test('an empty deletion set plans nothing, whatever the rows say', () => {
  const rows = [wt('s1', TREE, 'me'), wt('s2', REPO, 'gone')]
  assert.deepEqual(plan(rows, []), { removable: [], stale: [] })
})
