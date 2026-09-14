import * as fs from 'fs'
import * as path from 'path'
import type { MemoryFact, MemoryFactList } from '../shared/types'

// Retrieval-memory facts are one markdown file per fact, the file name being
// the fact id, under two roots. This mirrors
// cluxmate/core/retrieval_memory.py::_facts_dir — the Python side reads ONLY
// these two directories, and it indexes every direct *.md child
// (retrieval_memory.py: `for p in sorted(d.glob("*.md"))`, id = file stem), so
// the name shape does NOT decide recall: a hand-dropped notes.md is recalled
// just like a remember-tool fact and must therefore be listed here too.
export const FACT_SUFFIX = '.md'
export const FACT_MAX_BYTES = 8 * 1024
export const FACT_LIST_MAX = 200

// Anything readdir hands us ending in .md, except the bare '.md' itself. Names
// can never contain a path separator, so this cannot escape the directory.
export function isFactFile(name: string): boolean {
  return name.length > FACT_SUFFIX.length && name.endsWith(FACT_SUFFIX)
}

export function factsDir(homeDir: string, cwd: string, scope: 'global' | 'project'): string {
  return scope === 'global'
    ? path.join(homeDir, '.cluxmate', 'memory', 'global', 'facts')
    : path.join(cwd, '.cluxmate', 'memory', 'facts')
}

// Roots to scan, global first. With no project cwd (no session open) only the
// global root is listed: passing '' on would silently resolve the project root
// against the app's own working directory.
export function memoryRoots(
  homeDir: string,
  cwd: string,
): { root: string; scope: 'global' | 'project' }[] {
  const roots: { root: string; scope: 'global' | 'project' }[] = [
    { root: factsDir(homeDir, cwd, 'global'), scope: 'global' },
  ]
  if (cwd) roots.push({ root: factsDir(homeDir, cwd, 'project'), scope: 'project' })
  return roots
}

// Bounded read: a fact is normally a few hundred bytes, but nothing stops a
// user from dropping a huge file in there — read at most FACT_MAX_BYTES so one
// bad file cannot push a giant string through IPC.
function readFact(full: string, id: string, scope: 'global' | 'project'): MemoryFact | null {
  let fd: number
  try {
    fd = fs.openSync(full, 'r')
  } catch {
    return null // gone or unreadable — skip this entry only
  }
  try {
    const st = fs.fstatSync(fd)
    if (!st.isFile()) return null
    const buf = Buffer.alloc(FACT_MAX_BYTES)
    const read = fs.readSync(fd, buf, 0, FACT_MAX_BYTES, 0)
    const truncated = st.size > read
    let body = buf.subarray(0, read).toString('utf-8')
    if (truncated) body += '\n\n[truncated]'
    return { id, scope, body, truncated, mtimeMs: st.mtimeMs }
  } catch {
    return null
  } finally {
    try {
      fs.closeSync(fd)
    } catch {
      /* already closed */
    }
  }
}

export function scanFacts(homeDir: string, cwd: string): MemoryFactList {
  const facts: MemoryFact[] = []
  for (const { root, scope } of memoryRoots(homeDir, cwd)) {
    let names: string[]
    try {
      names = fs.readdirSync(root)
    } catch {
      continue // no directory yet — no facts, not an error
    }
    for (const name of names) {
      if (!isFactFile(name)) continue
      const fact = readFact(path.join(root, name), name.slice(0, -FACT_SUFFIX.length), scope)
      if (fact) facts.push(fact)
    }
  }
  const total = facts.length
  facts.sort((a, b) => b.mtimeMs - a.mtimeMs)
  return { facts: facts.slice(0, FACT_LIST_MAX), total }
}

// Delete one fact by id (the file's stem). The id may not be empty and may not
// contain a path separator, but the real gate is the containment check below:
// the resolved file must sit DIRECTLY inside the scope's facts dir. The Python
// side's own `forget` tool only accepts 12-hex ids, so a hand-dropped file can
// only be removed from here.
export function deleteFact(
  homeDir: string,
  cwd: string,
  scope: string,
  id: string,
): MemoryFactList {
  if (scope !== 'global' && scope !== 'project') {
    throw new Error(`Invalid memory scope: ${String(scope)}`)
  }
  if (!id || id.includes('/') || id.includes('\\') || path.isAbsolute(id)) {
    throw new Error(`Invalid fact id: ${String(id)}`)
  }
  if (scope === 'project' && !cwd) {
    throw new Error('No project directory for a project fact')
  }
  const root = factsDir(homeDir, cwd, scope)
  const full = path.join(root, `${id}${FACT_SUFFIX}`)
  if (path.dirname(path.resolve(full)) !== path.resolve(root)) {
    throw new Error('Refusing to delete outside the memory root')
  }
  try {
    fs.unlinkSync(full)
  } catch (e: any) {
    // Already gone (removed by hand, or by the agent's own forget tool).
    if (e?.code !== 'ENOENT') throw e
  }
  return scanFacts(homeDir, cwd)
}
