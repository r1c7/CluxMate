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

import { isInsideOrEqual, sessionsInTree } from '../src/main/worktree-guard.ts'

// Synthetic, platform-native absolute paths: the guard is pure path/string logic
// and never touches the filesystem, so a real tree on disk would only add a
// temp-dir dependency (and a cleanup failure mode) to the test.
const REPO = path.resolve('cluxmate-guard-fixture', 'repo')
const TREE = path.join(REPO, '.worktrees', 'me')

const row = (id: string, cwd: string, title = id) => ({ id, cwd, title })

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
