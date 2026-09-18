// Unit tests for the worktree-container filtering rule — the desktop's half of
// "CluxMate's own `.worktrees/` is never one of the user's uncommitted changes".
// Run from desktop/: npm test
//
// The module under test is PURE (no electron, no fs, no child process) by
// design, which is what lets this file load it with Node's type-stripping
// runner; main/git-service.ts, which supplies the facts and runs git, cannot be
// loaded here. The Python half of the same rule (`_dirty_files`, `_container_ignored`)
// is covered by tests/core/test_worktree.py.
import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  NO_CONTAINER,
  commitGuardRefusal,
  containerActive,
  isContainerLine,
  withoutContainerLines,
  type WorktreeContainer,
} from '../src/shared/worktree-container.ts'

const CONTAINER: WorktreeContainer = { name: '.worktrees', ignored: true }

test('an ignored container filters its own rows and only those', () => {
  const lines = [' M a.txt', '.worktrees/me/wip.txt', '.worktrees', '?? src/new.ts']
  assert.deepEqual(withoutContainerLines(CONTAINER, lines), [' M a.txt', '?? src/new.ts'])
})

test('a container git does NOT ignore is kept in the listing', () => {
  // The whole point of the `ignored` flag: on such a repository that row is a
  // real change, and the dialog's warning about it is correct.
  const container: WorktreeContainer = { name: '.worktrees', ignored: false }
  const lines = [' M a.txt', '.worktrees/']
  assert.deepEqual(withoutContainerLines(container, lines), lines)
})

test('an unknown container filters nothing', () => {
  const lines = [' M a.txt', '.worktrees/me/wip.txt']
  assert.deepEqual(withoutContainerLines(NO_CONTAINER, lines), lines)
  assert.equal(containerActive(NO_CONTAINER), false)
  assert.equal(containerActive(CONTAINER), true)
  // A name without the ignore flag is not "active": filtering on a guess would
  // hide a change the user may have to deal with.
  assert.equal(containerActive({ name: '.worktrees', ignored: false }), false)
  assert.equal(containerActive({ name: '', ignored: true }), false)
})

test('a path that merely shares the container prefix is a different directory', () => {
  assert.equal(isContainerLine(CONTAINER, '.worktrees-old/wip.txt'), false)
  assert.equal(isContainerLine(CONTAINER, '.worktreesX'), false)
  assert.equal(isContainerLine(CONTAINER, '.worktrees'), true)
  assert.equal(isContainerLine(CONTAINER, '.worktrees/'), true)
  assert.equal(isContainerLine(CONTAINER, '.worktrees/me/wip.txt'), true)
})

test('both separators and surrounding whitespace are folded', () => {
  // `git status --porcelain` writes the path relative to the tree with the
  // platform's separator; the listing may also pass through a split/trim.
  assert.equal(isContainerLine(CONTAINER, '.worktrees\\me\\wip.txt'), true)
  assert.equal(isContainerLine(CONTAINER, '  .worktrees/me  '), true)
  // A SIBLING of the container (`me` next to it, not inside it) is untouched —
  // the same distinction `isInsideOrEqual` draws for absolute paths.
  assert.equal(isContainerLine(CONTAINER, 'me\\wip.txt'), false)
})

test('the container NAME is what the rule keys on, never a hardcoded path', () => {
  const renamed: WorktreeContainer = { name: '.cluxmate-worktrees', ignored: true }
  assert.deepEqual(withoutContainerLines(renamed, ['.worktrees/me/x', '.cluxmate-worktrees/x']), [
    '.worktrees/me/x',
  ])
})

test('filtering returns a new array and leaves the input untouched', () => {
  const lines = ['.worktrees/me/wip.txt']
  const out = withoutContainerLines(CONTAINER, lines)
  assert.deepEqual(out, [])
  assert.deepEqual(lines, ['.worktrees/me/wip.txt'])
  assert.notEqual(out, lines)
  // The unfiltered branch copies too: a caller mutating what it gets back must
  // not reach into a listing it did not own.
  const kept = withoutContainerLines(NO_CONTAINER, lines)
  assert.notEqual(kept, lines)
})

test('a real change alongside the container still marks the tree dirty', () => {
  // The bug this replaces was a whole-listing judgement (`trim().length > 0`),
  // so the interesting case is the MIX: filtering must not turn real dirt into
  // "clean".
  const lines = [' M a.txt', '.worktrees/me/wip.txt']
  assert.notDeepEqual(withoutContainerLines(CONTAINER, lines), [])
  assert.deepEqual(withoutContainerLines(CONTAINER, ['.worktrees/me/wip.txt', '']), [''])
})

test('the commit guard refuses only an unignored container git confirms', () => {
  // Git says "not ignored" ⇒ refuse: `add -A` would record an embedded
  // repository (a 160000 gitlink) that restore cannot take back.
  const refusal = commitGuardRefusal(CONTAINER, false)
  assert.equal(typeof refusal, 'string')
  assert.match(refusal as string, /\.worktrees\//)

  // Git says "ignored" ⇒ go ahead, whatever the older snapshot claimed: the
  // snapshot is an `info` call old and cannot see an ignore removed since.
  assert.equal(commitGuardRefusal(CONTAINER, true), null)
  assert.equal(commitGuardRefusal({ name: '.worktrees', ignored: false }, true), null)
  // Nothing known to protect (older CLI / not a repository) ⇒ go ahead.
  assert.equal(commitGuardRefusal(NO_CONTAINER, false), null)
})

test('the guard message names the directory to fix', () => {
  const refusal = commitGuardRefusal({ name: '.cluxmate-worktrees', ignored: false }, false)
  assert.match(refusal as string, /\.cluxmate-worktrees\//)
  assert.match(refusal as string, /\.gitignore/)
})
