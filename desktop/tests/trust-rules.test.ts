// Unit tests for the shared project-trust rules (imported by both the main
// process and the renderer store). Run from desktop/:  npm test
//
// Node's built-in runner executes .ts directly (type stripping). The import
// carries the .ts extension because Node resolves it itself, which is why this
// file lives in tests/ rather than beside the module.
import test from 'node:test'
import assert from 'node:assert/strict'

import { isUntrusted, shouldPromptTrust, trustSummary, trustChangeRestartsBridge, bridgesToRestart, planTrustCall } from '../src/shared/trust-rules.ts'

const snapshot = (status: string, findings: { kind: string; label: string; path: string }[] = []) => ({
  cwd: 'E:\\proj', status, source: 'registry', findings, store: {},
})

test('a trusted directory is never untrusted and never prompts', () => {
  assert.equal(isUntrusted(snapshot('trusted', [{ kind: 'hooks', label: 'Hooks', path: '.cluxmate/settings.json' }])), false)
  assert.equal(shouldPromptTrust(snapshot('trusted')), false)
})

test('unknown with findings prompts', () => {
  const s = snapshot('unknown', [{ kind: 'hooks', label: 'Hooks (settings.json)', path: '.cluxmate/settings.json' }])
  assert.equal(shouldPromptTrust(s), true)
  assert.equal(isUntrusted(s), true)
})

test('unknown without findings never prompts and is not gated', () => {
  assert.equal(shouldPromptTrust(snapshot('unknown')), false)
  assert.equal(isUntrusted(snapshot('unknown')), false)
})

test('denied with findings is untrusted but does not prompt again', () => {
  const s = snapshot('denied', [{ kind: 'mcp', label: 'MCP servers (mcp.json)', path: '.cluxmate/mcp.json' }])
  assert.equal(isUntrusted(s), true)
  assert.equal(shouldPromptTrust(s), false)
})

test('a null snapshot is neither', () => {
  assert.equal(isUntrusted(null), false)
  assert.equal(shouldPromptTrust(null), false)
})

test('trustSummary lists labels then paths', () => {
  const s = snapshot('unknown', [
    { kind: 'hooks', label: 'Hooks (settings.json)', path: '.cluxmate/settings.json' },
    { kind: 'mcp', label: 'MCP servers (mcp.json)', path: '.cluxmate/mcp.json' },
  ])
  assert.equal(trustSummary(s), 'Hooks (settings.json), MCP servers (mcp.json)')
})

// ── the bridge-restart decision (the main process's kill rule) ──────────────
// The main process has no Electron harness here, so the decision itself is the
// pure function it calls: `sameDir` stands in for its realpath comparison.
const sameDir = (a: string, b: string) =>
  a.toLowerCase().replace(/[\\/]+$/, '') === b.toLowerCase().replace(/[\\/]+$/, '')

const restarts = (before: string | null, after: string | null, cwd = 'E:\\proj', liveCwd: string | null = 'E:\\proj') =>
  trustChangeRestartsBridge(before, after, cwd, liveCwd, sameDir)

test('every decision change tears the running process down', () => {
  // Revoking is the case that used to be a no-op: the agent kept running the
  // hooks / MCP clients / skills it had loaded while trusted.
  assert.equal(restarts('trusted', 'denied'), true)
  assert.equal(restarts('trusted', 'unknown'), true) // trust/remove
  assert.equal(restarts('denied', 'unknown'), true) // trust/remove after a denial
  assert.equal(restarts('unknown', 'trusted'), true)
  assert.equal(restarts('unknown', 'denied'), true)
  assert.equal(restarts('denied', 'trusted'), true)
})

test('an unchanged answer leaves the warm process alone', () => {
  for (const status of ['trusted', 'denied', 'unknown']) {
    assert.equal(restarts(status, status), false)
  }
})

test('a change to another directory does not cost this session its process', () => {
  // Editing an unrelated registry entry: the live bridge is running elsewhere.
  assert.equal(
    trustChangeRestartsBridge('trusted', 'denied', 'E:\\other', 'E:\\proj', sameDir),
    false,
  )
})

test('nothing loaded means nothing to tear down', () => {
  assert.equal(restarts('trusted', 'denied', 'E:\\proj', null), false)
  assert.equal(restarts(null, 'denied'), false)
  assert.equal(restarts('trusted', null), false)
})

// ── which live bridges a write tears down, across sessions ─────────────────
// The Settings list can revoke any recorded row while another session is the
// active one, so the decision is about the DIRECTORY, not about the caller.
const liveBridges = (...bridges: [string, string | null][]) =>
  bridges.map(([sessionId, cwd]) => ({ sessionId, cwd }))

test('a revoke kills every live bridge running in that directory', () => {
  // Session B revoked X while session A is the one running in X: A's process is
  // the one executing X's hooks / MCP clients / skills.
  assert.deepEqual(
    bridgesToRestart('trusted', 'denied', 'E:\\proj', liveBridges(['a', 'E:\\proj'], ['b', 'E:\\other']), sameDir),
    ['a'],
  )
  // Two sessions in the same directory both go; a variant spelling still counts.
  assert.deepEqual(
    bridgesToRestart('trusted', 'denied', 'E:\\proj', liveBridges(['a', 'E:\\proj'], ['c', 'E:\\PROJ\\'], ['d', 'E:\\elsewhere']), sameDir),
    ['a', 'c'],
  )
})

test('a foreign-cwd edit kills nothing', () => {
  assert.deepEqual(
    bridgesToRestart('trusted', 'denied', 'E:\\other', liveBridges(['a', 'E:\\proj'], ['b', 'E:\\third']), sameDir),
    [],
  )
})

test('an unchanged answer, an unknown bridge dir or no update all kill nothing', () => {
  for (const status of ['trusted', 'denied', 'unknown']) {
    assert.deepEqual(bridgesToRestart(status, status, 'E:\\proj', liveBridges(['a', 'E:\\proj']), sameDir), [])
  }
  assert.deepEqual(bridgesToRestart('trusted', 'denied', 'E:\\proj', [], sameDir), [])
  assert.deepEqual(bridgesToRestart('trusted', 'denied', 'E:\\proj', liveBridges(['a', null]), sameDir), [])
  assert.deepEqual(bridgesToRestart(null, 'denied', 'E:\\proj', liveBridges(['a', 'E:\\proj']), sameDir), [])
})

// ── the trust-call plan: which directory the session's bridge serves ────────
// Settings can revoke a row for a directory that is NOT the active session's,
// and the call is answered by the active session's own bridge. The plan is what
// keeps the target out of that bridge's spawn directory: warming at the target
// would make ensureBridge respawn the live process over there.
test('a foreign trust target never moves the session bridge', () => {
  for (const target of ['E:\\other', 'E:\\proj\\nested', 'C:\\', '', 'E:/OTHER']) {
    const plan = planTrustCall(target, 'E:\\proj', sameDir)
    assert.equal(plan.warmCwd, 'E:\\proj')
    assert.equal(plan.targetIsSessionDir, false)
  }
})

test('a directory this session does not run in never files a session decision', () => {
  // sessionTrust is only read back for the directory the session spawns at, so
  // an entry for a foreign target would be a decision with no consumer.
  assert.equal(planTrustCall('E:\\other', 'E:\\proj', sameDir).targetIsSessionDir, false)
  // A session with no directory (or no record) files nothing either.
  assert.equal(planTrustCall('E:\\other', null, sameDir).targetIsSessionDir, false)
  assert.equal(planTrustCall('E:\\other', '', sameDir).targetIsSessionDir, false)
  assert.equal(planTrustCall('', '', sameDir).targetIsSessionDir, false)
})

test('the session directory itself is the one target that may file a decision', () => {
  for (const target of ['E:\\proj', 'E:\\proj\\', 'e:\\proj', 'E:\\PROJ']) {
    const plan = planTrustCall(target, 'E:\\proj', sameDir)
    assert.equal(plan.warmCwd, 'E:\\proj')
    assert.equal(plan.targetIsSessionDir, true)
  }
})

test('revoking a foreign row leaves the live process where it is', () => {
  // plan.warmCwd is the directory the live bridge runs in, so the restart rule
  // applied to it is exactly what the handler decides after the write: the row's
  // answer changed, but not the answer for the directory holding the process.
  const plan = planTrustCall('E:\\other', 'E:\\proj', sameDir)
  assert.equal(trustChangeRestartsBridge('trusted', 'unknown', 'E:\\other', plan.warmCwd, sameDir), false)
  assert.equal(trustChangeRestartsBridge('denied', 'unknown', 'E:\\other', plan.warmCwd, sameDir), false)
  assert.equal(plan.targetIsSessionDir, false)
})

test('revoking the session directory still tears the live process down', () => {
  const plan = planTrustCall('E:\\proj', 'E:\\proj', sameDir)
  assert.equal(trustChangeRestartsBridge('trusted', 'unknown', 'E:\\proj', plan.warmCwd, sameDir), true)
  assert.equal(trustChangeRestartsBridge('denied', 'unknown', 'E:\\proj', plan.warmCwd, sameDir), true)
  assert.equal(plan.targetIsSessionDir, true)
})
