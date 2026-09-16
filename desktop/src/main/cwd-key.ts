// Canonical key for the working directories the main process keys its own
// per-cwd state by (the session-only trust decisions). Every other layer
// normalizes: the `trust/get|set` snapshots carry `cwd = canonical(cwd)` from
// cluxmate/core/trust.py, so a trailing separator, a differently-cased drive
// letter or a symlinked spelling of one directory must not open a second key
// here — a session-only decision filed under a variant spelling would be
// silently dropped on the next respawn and lost.
//
// `realpathSync.native` resolves symlinks and restores the on-disk casing, which
// is what Path.resolve() does on the Python side; a path that no longer exists
// degrades to its absolute form, Path.resolve()'s own fallback.
//
// Pure filesystem + string logic (no electron imports) so desktop/tests can
// exercise it directly with the Node test runner.

import * as fs from 'fs'
import * as path from 'path'

export function canonicalCwdKey(cwd: string): string {
  if (!cwd) return ''
  try {
    return fs.realpathSync.native(cwd)
  } catch {
    return path.resolve(cwd)
  }
}
