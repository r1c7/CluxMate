// Unit tests for the pure worktree rules: how the main process reads the
// `cluxmate worktree … --json` output (src/main/worktree.ts) and how the
// renderer decides that a session lives in a worktree (SessionList/ContextMenu).
//
// Node's built-in runner executes .ts directly (type stripping), and the import
// carries the .ts extension because Node resolves it itself — see
// desktop/tests/skill-rules.test.ts. The module under test imports nothing, so
// these tests never spawn a process.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  branchPillLabel,
  canCreateWorktree,
  canSwitchBranches,
  isWorktreeSession,
  parseCluxmateJson,
  plainSessionCwd,
  spawnErrorMessage,
  withoutWorktreeBranches,
  worktreeBadgeLabel,
} from '../src/shared/worktree-rules.ts'

// §A's `info` payload, verbatim.
const INFO = '{"ok": true, "cwd": "E:\\\\repo", "root": "E:\\\\repo", "config_root": "E:\\\\repo", "is_worktree": false, "branch": "master"}'

// A session row from before §C's columns existed: neither worktree field is
// present at all. Bound to a name rather than inlined, so the call is not a
// fresh object literal (which would trip TS's excess-property check).
const PRE_WORKTREE_SESSION = { id: 's1', cwd: 'E:\\repo' }

test('a single JSON line on stdout is the payload', () => {
  const r = parseCluxmateJson(`${INFO}\n`, '', 0)
  assert.equal(r.ok, true)
  assert.deepEqual(r, {
    ok: true, cwd: 'E:\\repo', root: 'E:\\repo', config_root: 'E:\\repo',
    is_worktree: false, branch: 'master',
  })
})

test('the LAST non-empty line wins when noise precedes the payload', () => {
  const stdout = 'warning: something on stderr-ish stdout\n\n{"ok":true,"worktrees":[]}\n\n'
  assert.deepEqual(parseCluxmateJson(stdout, '', 0), { ok: true, worktrees: [] })
})

test('CRLF line endings do not defeat the parse', () => {
  assert.deepEqual(parseCluxmateJson('{"ok":true,"name":"me"}\r\n', '', 0), { ok: true, name: 'me' })
})

// The CLI's design: a failure is still exactly one line of JSON, on STDOUT, with
// exit code 1 — so the payload decides, never the exit code.
test('a failure payload is returned as-is, whatever the exit code says', () => {
  const line = '{"ok":false,"error":"dirty","message":"the main worktree has uncommitted changes","details":{"files":["a.txt"]}}'
  assert.deepEqual(parseCluxmateJson(`${line}\n`, '', 1), {
    ok: false, error: 'dirty', message: 'the main worktree has uncommitted changes',
    details: { files: ['a.txt'] },
  })
  assert.deepEqual(parseCluxmateJson(`${line}\n`, '', 0), {
    ok: false, error: 'dirty', message: 'the main worktree has uncommitted changes',
    details: { files: ['a.txt'] },
  })
})

test('a failure payload without details keeps its two required keys', () => {
  const r = parseCluxmateJson('{"ok":false,"error":"not-a-repo","message":"not a git repository"}\n', '', 1)
  assert.deepEqual(r, { ok: false, error: 'not-a-repo', message: 'not a git repository' })
})

// A python that dies before printing (import error, traceback) leaves stdout
// empty; the diagnostic is then only on stderr and must survive into `message`.
test('empty stdout + stderr content is bad-output carrying that stderr', () => {
  const r = parseCluxmateJson('', 'Traceback (most recent call last):\nImportError: boom\n', 1)
  assert.equal(r.ok, false)
  if (r.ok) return
  assert.equal(r.error, 'bad-output')
  assert.match(r.message, /ImportError: boom/)
})

test('whitespace-only stdout is as empty as no stdout', () => {
  const r = parseCluxmateJson('\n  \n', 'python: not found', 127)
  assert.equal(r.ok, false)
  if (r.ok) return
  assert.equal(r.error, 'bad-output')
  assert.match(r.message, /python: not found/)
})

// No stderr to fall back on ⇒ the offending line itself is the diagnostic.
test('a non-JSON line with no stderr is bad-output quoting that line', () => {
  const r = parseCluxmateJson('cluxmate: command not found\n', '', 127)
  assert.equal(r.ok, false)
  if (r.ok) return
  assert.equal(r.error, 'bad-output')
  assert.match(r.message, /cluxmate: command not found/)
})

test('no stdout and no stderr still yields a usable message', () => {
  const r = parseCluxmateJson('', '', 1)
  assert.equal(r.ok, false)
  if (r.ok) return
  assert.equal(r.error, 'bad-output')
  assert.match(r.message, /1/)
})

// Only a JSON OBJECT is a payload: an array or a scalar is valid JSON and still
// not something any caller can read `ok` off.
test('valid JSON that is not an object is rejected', () => {
  for (const line of ['[]', '"x"', '123', 'null', 'true', '[{"ok":true}]']) {
    const r = parseCluxmateJson(line, '', 0)
    assert.equal(r.ok, false, `${line} must not be accepted`)
    if (r.ok) continue
    assert.equal(r.error, 'bad-output')
    assert.ok(r.message.length > 0)
  }
})

// A traceback is long and its useful line is the LAST one, so the message keeps
// the stderr tail instead of pasting kilobytes into a dialog.
test('a long stderr contributes only its tail', () => {
  const stderr = `${'x'.repeat(4000)}\nRuntimeError: the real failure\n`
  const r = parseCluxmateJson('', stderr, 1)
  assert.equal(r.ok, false)
  if (r.ok) return
  assert.match(r.message, /RuntimeError: the real failure/)
  assert.ok(r.message.length < 1000, `message too long: ${r.message.length}`)
})

test('spawnErrorMessage surfaces the OS detail and names the ENOENT cases', () => {
  assert.match(spawnErrorMessage(Object.assign(new Error('spawn python ENOENT'), { code: 'ENOENT' })), /python/i)
  assert.equal(spawnErrorMessage(new Error('EACCES: permission denied')), 'EACCES: permission denied')
  assert.ok(spawnErrorMessage(undefined).length > 0)
})

// --- the session shape the renderer keys on (§C's two columns) ---

test('worktreeBadgeLabel is the worktree name, and null without one', () => {
  assert.equal(worktreeBadgeLabel({ worktree_name: 'me', worktree_branch: 'cluxmate/me' }), 'me')
  assert.equal(worktreeBadgeLabel({ worktree_name: 'me', worktree_branch: null }), 'me')
  assert.equal(worktreeBadgeLabel({ worktree_name: '  me  ' }), 'me')
})

test('a blank, null or absent worktree_name is no worktree badge', () => {
  assert.equal(worktreeBadgeLabel({ worktree_name: null, worktree_branch: 'cluxmate/me' }), null)
  assert.equal(worktreeBadgeLabel({ worktree_name: '', worktree_branch: 'cluxmate/me' }), null)
  assert.equal(worktreeBadgeLabel({ worktree_name: '   ' }), null)
  assert.equal(worktreeBadgeLabel(PRE_WORKTREE_SESSION), null)
  assert.equal(worktreeBadgeLabel(null), null)
  assert.equal(worktreeBadgeLabel(undefined), null)
})

test('isWorktreeSession gates on the name, the column the badge and menu use', () => {
  assert.equal(isWorktreeSession({ worktree_name: 'me', worktree_branch: 'cluxmate/me' }), true)
  assert.equal(isWorktreeSession({ worktree_name: '', worktree_branch: 'cluxmate/me' }), false)
  assert.equal(isWorktreeSession({ worktree_name: null, worktree_branch: null }), false)
  // A pre-worktree session row: the columns are absent entirely.
  assert.equal(isWorktreeSession(PRE_WORKTREE_SESSION), false)
})

// --- the directory a new plain session starts in ---

// §E: `project_root` differs from `cwd` exactly when the session runs inside a
// linked worktree, so "equal" covers a plain repo, a plain-repo subdirectory and
// a non-git directory — all of which must keep inheriting the exact directory.
test('plainSessionCwd keeps an inherited directory that is not a worktree', () => {
  assert.equal(plainSessionCwd('E:\\repo', { cwd: 'E:\\repo', project_root: 'E:\\repo' }), 'E:\\repo')
  assert.equal(
    plainSessionCwd('E:\\repo\\pkg', { cwd: 'E:\\repo\\pkg', project_root: 'E:\\repo\\pkg' }),
    'E:\\repo\\pkg',
  )
})

test('plainSessionCwd sends a worktree checkout to its project root', () => {
  assert.equal(
    plainSessionCwd('E:\\repo\\.worktrees\\me', { cwd: 'E:\\repo\\.worktrees\\me', project_root: 'E:\\repo' }),
    'E:\\repo',
  )
  // ...including a session whose cwd is a SUBDIRECTORY of the tree.
  assert.equal(
    plainSessionCwd('E:\\repo\\.worktrees\\me\\src', {
      cwd: 'E:\\repo\\.worktrees\\me\\src',
      project_root: 'E:\\repo',
    }),
    'E:\\repo',
  )
})

test('plainSessionCwd leaves the inherited path alone when no row owns it or the row is incomplete', () => {
  const gone = 'E:\\gone\\.worktrees\\me'
  assert.equal(plainSessionCwd(gone, null), gone)
  assert.equal(plainSessionCwd(gone, undefined), gone)
  // A row from before `project_root` (or a blank one) tells us nothing.
  assert.equal(plainSessionCwd('E:\\repo', { cwd: 'E:\\repo' }), 'E:\\repo')
  assert.equal(plainSessionCwd('E:\\repo', { cwd: 'E:\\repo', project_root: '   ' }), 'E:\\repo')
  assert.equal(plainSessionCwd('E:\\repo', { project_root: 'E:\\repo' }), 'E:\\repo')
})

// --- which branches the project may switch to ---

test('canSwitchBranches is false inside a linked worktree, true in the project tree', () => {
  assert.equal(canSwitchBranches({ inRepo: true, isWorktree: false }), true)
  assert.equal(canSwitchBranches({ inRepo: true, isWorktree: true }), false)
})

test('canSwitchBranches hides the pill for anything that is not a repo, and for missing state', () => {
  assert.equal(canSwitchBranches(null), false)
  assert.equal(canSwitchBranches(undefined), false)
  assert.equal(canSwitchBranches({ inRepo: false, isWorktree: false }), false)
  assert.equal(canSwitchBranches({ inRepo: false, isWorktree: true }), false)
})

// Fail-safe on the newest field: a main process that does not report
// `isWorktree` yet keeps today's behaviour (the pill is shown).
test('an absent isWorktree field does not hide the pill', () => {
  assert.equal(canSwitchBranches({ inRepo: true }), true)
})

// The pill is DISABLED inside a worktree, not gone: it names the tree.
test('branchPillLabel is the branch in the project tree and the TREE name in a worktree', () => {
  assert.equal(branchPillLabel({ currentBranch: 'master', isWorktree: false }), 'master')
  assert.equal(branchPillLabel({ currentBranch: 'cluxmate/me', isWorktree: true, worktreeName: 'me' }), 'me')
})

test('branchPillLabel falls back to the branch, and to null when there is nothing to name', () => {
  // A tree whose name could not be read must not blank the pill either.
  assert.equal(branchPillLabel({ currentBranch: 'cluxmate/me', isWorktree: true, worktreeName: '' }), 'cluxmate/me')
  assert.equal(branchPillLabel({ currentBranch: 'x', isWorktree: true, worktreeName: null }), 'x')
  assert.equal(branchPillLabel({ currentBranch: '  ', isWorktree: false }), null)
  assert.equal(branchPillLabel({ isWorktree: true }), null)
  assert.equal(branchPillLabel(null), null)
  assert.equal(branchPillLabel(undefined), null)
})

// --- which projects may offer "new session in a git worktree" ---

test('canCreateWorktree withholds the entry only for a CONFIRMED non-repository', () => {
  assert.equal(canCreateWorktree(true), true)
  assert.equal(canCreateWorktree(false), false)
  // Not answered yet (the probe is in flight, or the group has no path): today's
  // behaviour, and the CLI's own `not-a-repo` stays the backstop.
  assert.equal(canCreateWorktree(undefined), true)
  assert.equal(canCreateWorktree(null), true)
})

// `cluxmate worktree list --json` rows, minus the fields this rule ignores.
const ROWS = [
  { path: 'E:\\repo', branch: 'master', is_main: true },
  { path: 'E:\\repo\\.worktrees\\me', branch: 'cluxmate/me', is_main: false },
  { path: 'E:\\repo\\.worktrees\\two', branch: 'cluxmate/two', is_main: false },
]

test('withoutWorktreeBranches drops exactly the branches a linked worktree holds', () => {
  assert.deepEqual(
    withoutWorktreeBranches(['master', 'cluxmate/me', 'cluxmate/two', 'feature'], ROWS),
    ['master', 'feature'],
  )
})

// The main worktree is a row too, and its branch is the ordinary current branch.
test('the main worktree row never hides its own branch', () => {
  assert.deepEqual(withoutWorktreeBranches(['master', 'feature'], [ROWS[0]]), ['master', 'feature'])
})

// A tree removed with --keep-branch is no longer in `list`, so its branch is an
// ordinary branch again — which is exactly what keeping it is for (Phase 2's
// 「保留成分支」: the user merges or switches to it from the project tree).
test('a branch kept from a removed worktree stays switchable', () => {
  assert.deepEqual(
    withoutWorktreeBranches(['master', 'cluxmate/kept'], [ROWS[0]]),
    ['master', 'cluxmate/kept'],
  )
})

// A detached HEAD row carries no branch, and an unusable list (older CLI, no
// python) is null/absent/empty: all of them filter nothing.
test('rows without a branch and unusable lists filter nothing', () => {
  const branches = ['master', 'cluxmate/me']
  assert.deepEqual(withoutWorktreeBranches(branches, [{ is_main: false }]), branches)
  assert.deepEqual(withoutWorktreeBranches(branches, [{ branch: '', is_main: false }]), branches)
  assert.deepEqual(withoutWorktreeBranches(branches, []), branches)
  assert.deepEqual(withoutWorktreeBranches(branches, null), branches)
  assert.deepEqual(withoutWorktreeBranches(branches, undefined), branches)
})

test('withoutWorktreeBranches preserves order and leaves its input alone', () => {
  const input = ['z', 'cluxmate/me', 'a']
  assert.deepEqual(withoutWorktreeBranches(input, ROWS), ['z', 'a'])
  assert.deepEqual(input, ['z', 'cluxmate/me', 'a'])
})
