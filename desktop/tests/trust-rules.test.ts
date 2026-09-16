// Unit tests for the shared project-trust rules (imported by both the main
// process and the renderer store). Run from desktop/:  npm test
//
// Node's built-in runner executes .ts directly (type stripping). The import
// carries the .ts extension because Node resolves it itself, which is why this
// file lives in tests/ rather than beside the module.
import test from 'node:test'
import assert from 'node:assert/strict'

import { isUntrusted, shouldPromptTrust, trustSummary } from '../src/shared/trust-rules.ts'

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
