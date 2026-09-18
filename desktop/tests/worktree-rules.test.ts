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
  isWorktreeSession,
  parseCluxmateJson,
  spawnErrorMessage,
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
