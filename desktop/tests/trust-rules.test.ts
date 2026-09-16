// Unit tests for the shared project-trust rules (imported by both the main
// process and the renderer store). Run from desktop/:  npm test
//
// Node's built-in runner executes .ts directly (type stripping). The import
// carries the .ts extension because Node resolves it itself, which is why this
// file lives in tests/ rather than beside the module.
import test from 'node:test'
import assert from 'node:assert/strict'

import { isUntrusted, shouldPromptTrust, trustSummary, trustChangeRestartsBridge } from '../src/shared/trust-rules.ts'

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
