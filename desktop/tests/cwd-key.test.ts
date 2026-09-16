// Unit tests for the main process's canonical cwd key — the one spelling the
// session-only trust decisions are filed under, and the key a respawn looks
// them up with. Run from desktop/:  npm test
//
// Node's built-in runner executes .ts directly (type stripping). The import
// carries the .ts extension because Node resolves it itself, which is why this
// file lives in tests/ rather than beside the module.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { canonicalCwdKey } from '../src/main/cwd-key.ts'

function fixture(): { dir: string; cleanup: () => void } {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'cluxmate-cwd-key-'))
  const dir = path.join(base, 'project')
  fs.mkdirSync(dir, { recursive: true })
  return { dir, cleanup: () => fs.rmSync(base, { recursive: true, force: true }) }
}

test('separator, dot-segment and slash variants are the same key', () => {
  const f = fixture()
  try {
    const key = canonicalCwdKey(f.dir)
    assert.equal(canonicalCwdKey(f.dir + path.sep), key)
    assert.equal(canonicalCwdKey(f.dir + path.sep + '.'), key)
    assert.equal(canonicalCwdKey(f.dir.replace(/[\\/]/g, '/')), key)
    assert.equal(canonicalCwdKey(path.join(f.dir, '..', path.basename(f.dir))), key)
  } finally {
    f.cleanup()
  }
})

test('a differently-cased spelling is the same key', () => {
  const f = fixture()
  try {
    // The case differences the renderer can hand over: a drive letter picked
    // from a folder dialog versus one typed by hand.
    assert.equal(canonicalCwdKey(f.dir.toUpperCase()), canonicalCwdKey(f.dir))
    assert.equal(canonicalCwdKey(f.dir.toLowerCase()), canonicalCwdKey(f.dir))
  } finally {
    f.cleanup()
  }
})

test('a symlinked spelling is the same key as its target', (t) => {
  const f = fixture()
  try {
    const link = path.join(path.dirname(f.dir), 'link')
    try {
      fs.symlinkSync(f.dir, link, 'dir')
    } catch {
      t.skip('symlinks unavailable on this platform')
      return
    }
    assert.equal(canonicalCwdKey(link), canonicalCwdKey(f.dir))
  } finally {
    f.cleanup()
  }
})

test('a directory that no longer exists still yields one stable absolute key', () => {
  const f = fixture()
  try {
    const gone = path.join(f.dir, 'gone')
    const key = canonicalCwdKey(gone)
    assert.equal(path.isAbsolute(key), true)
    assert.equal(canonicalCwdKey(gone + path.sep), key)
    assert.equal(canonicalCwdKey(gone + path.sep + '.'), key)
  } finally {
    f.cleanup()
  }
})

test('an empty cwd has no key', () => {
  // path.resolve('') would be the process cwd — a decision filed under it would
  // belong to no project at all.
  assert.equal(canonicalCwdKey(''), '')
})
