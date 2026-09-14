// Unit tests for the main-process retrieval-memory fact reader. Run from
// desktop/:  npm test
//
// Node's built-in runner executes .ts directly (type stripping; default since
// Node 23.6, flagged from 22.6 — measured on v24.12.0). The import below must
// carry the .ts extension because Node resolves it itself, which is the one
// reason this file lives in tests/ instead of beside the module: a project that
// typechecks it would need allowImportingTsExtensions. The module under test
// IS typechecked (tsconfig.main.json includes src/main/**).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import {
  FACT_LIST_MAX,
  FACT_MAX_BYTES,
  deleteFact,
  factsDir,
  isFactFile,
  memoryRoots,
  scanFacts,
} from '../src/main/memory-facts.ts'
const ID_A = 'aaaaaaaaaaaa'
const ID_B = 'bbbbbbbbbbbb'

// A throwaway home + project pair with both facts directories present.
function fixture(): { home: string; cwd: string; cleanup: () => void } {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'cluxmate-facts-'))
  const home = path.join(base, 'home')
  const cwd = path.join(base, 'project')
  fs.mkdirSync(factsDir(home, cwd, 'global'), { recursive: true })
  fs.mkdirSync(factsDir(home, cwd, 'project'), { recursive: true })
  return { home, cwd, cleanup: () => fs.rmSync(base, { recursive: true, force: true }) }
}

test('lists facts from both roots with scope and id', () => {
  const f = fixture()
  try {
    fs.writeFileSync(path.join(factsDir(f.home, f.cwd, 'global'), `${ID_A}.md`), 'global fact body\n')
    fs.writeFileSync(path.join(factsDir(f.home, f.cwd, 'project'), `${ID_B}.md`), 'project fact body\n')
    const out = scanFacts(f.home, f.cwd)
    assert.equal(out.total, 2)
    assert.equal(out.facts.length, 2)
    const byId = new Map(out.facts.map((x) => [x.id, x]))
    assert.equal(byId.get(ID_A)?.scope, 'global')
    assert.equal(byId.get(ID_A)?.body, 'global fact body\n')
    assert.equal(byId.get(ID_A)?.truncated, false)
    assert.equal(byId.get(ID_B)?.scope, 'project')
    assert.ok((byId.get(ID_A)?.mtimeMs ?? 0) > 0)
  } finally {
    f.cleanup()
  }
})

// The Python side indexes every direct *.md child of a facts dir
// (retrieval_memory.py: `d.glob("*.md")`, id = file stem), so the list must show
// those too — hiding a hand-dropped notes.md would hide a fact that IS recalled.
test('lists every .md file in the roots and nothing else', () => {
  const f = fixture()
  try {
    const dir = factsDir(f.home, f.cwd, 'global')
    fs.writeFileSync(path.join(dir, 'notes.md'), 'hand dropped')
    fs.writeFileSync(path.join(dir, 'AAAABBBBCCCC.md'), 'upper case hex')
    fs.writeFileSync(path.join(dir, 'UPPER.MD'), 'upper case suffix')
    fs.writeFileSync(path.join(dir, `${ID_A}.md.bak`), 'x')   // not .md
    fs.writeFileSync(path.join(dir, 'README.txt'), 'x')       // not .md
    fs.mkdirSync(path.join(dir, `${ID_B}.md`))                // a directory, not a file
    const out = scanFacts(f.home, f.cwd)
    // On Windows glob is case-insensitive, so UPPER.MD is a fact there and its
    // stem ('UPPER') is the id; on POSIX it is not a fact at all.
    const expected = ['AAAABBBBCCCC', 'notes'].concat(isFactFile('UPPER.MD') ? ['UPPER'] : [])
    assert.deepEqual(out.facts.map((x) => x.id).sort(), expected.sort())
    assert.equal(out.total, expected.length)
  } finally {
    f.cleanup()
  }
})

test('case-variant suffixes count only where python glob is case-insensitive', () => {
  assert.equal(isFactFile('notes.md', true), true)
  assert.equal(isFactFile('notes.md', false), true)
  assert.equal(isFactFile('NOTES.MD', true), true)    // Windows: fnmatch normcases
  assert.equal(isFactFile('NOTES.MD', false), false)  // POSIX: it does not
  assert.equal(isFactFile('MiXeD.Md', true), true)
  assert.equal(isFactFile('MiXeD.Md', false), false)
  assert.equal(isFactFile('.md', true), false)
  assert.equal(isFactFile('.md', false), false)
  assert.equal(isFactFile('notes.txt', true), false)
})

test('missing roots are empty, not an error', () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'cluxmate-facts-'))
  try {
    assert.deepEqual(scanFacts(path.join(base, 'nohome'), path.join(base, 'noproject')), {
      facts: [],
      total: 0,
    })
  } finally {
    fs.rmSync(base, { recursive: true, force: true })
  }
})

test('with no project cwd only the global root is scanned', () => {
  const f = fixture()
  try {
    fs.writeFileSync(path.join(factsDir(f.home, f.cwd, 'global'), `${ID_A}.md`), 'g')
    fs.writeFileSync(path.join(factsDir(f.home, f.cwd, 'project'), `${ID_B}.md`), 'p')
    const roots = memoryRoots(f.home, '')
    assert.deepEqual(roots.map((r) => r.scope), ['global'])
    // '' must NOT resolve the project root against the app's own cwd.
    assert.equal(roots[0].root, path.join(f.home, '.cluxmate', 'memory', 'global', 'facts'))
    const out = scanFacts(f.home, '')
    assert.equal(out.total, 1)
    assert.equal(out.facts[0].id, ID_A)
  } finally {
    f.cleanup()
  }
})

test('sorts newest first', () => {
  const f = fixture()
  try {
    const dir = factsDir(f.home, f.cwd, 'global')
    const a = path.join(dir, `${ID_A}.md`)
    const b = path.join(dir, `${ID_B}.md`)
    fs.writeFileSync(a, 'older')
    fs.writeFileSync(b, 'newer')
    fs.utimesSync(a, new Date(1000), new Date(1000))
    fs.utimesSync(b, new Date(2000), new Date(2000))
    assert.deepEqual(scanFacts(f.home, f.cwd).facts.map((x) => x.id), [ID_B, ID_A])
  } finally {
    f.cleanup()
  }
})

test('caps the list but still reports the real total', () => {
  const f = fixture()
  try {
    const dir = factsDir(f.home, f.cwd, 'global')
    for (let i = 0; i < FACT_LIST_MAX + 1; i++) {
      fs.writeFileSync(path.join(dir, `${i.toString(16).padStart(12, '0')}.md`), `fact ${i}`)
    }
    const out = scanFacts(f.home, f.cwd)
    assert.equal(out.total, FACT_LIST_MAX + 1)
    assert.equal(out.facts.length, FACT_LIST_MAX)
  } finally {
    f.cleanup()
  }
})

test('truncates an oversized body and flags it', () => {
  const f = fixture()
  try {
    const dir = factsDir(f.home, f.cwd, 'global')
    fs.writeFileSync(path.join(dir, `${ID_A}.md`), 'x'.repeat(FACT_MAX_BYTES + 10))
    fs.writeFileSync(path.join(dir, `${ID_B}.md`), 'y'.repeat(FACT_MAX_BYTES))
    const byId = new Map(scanFacts(f.home, f.cwd).facts.map((x) => [x.id, x]))
    const big = byId.get(ID_A)!
    assert.equal(big.truncated, true)
    assert.ok(big.body.endsWith('\n\n[truncated]'))
    assert.equal(big.body.length, FACT_MAX_BYTES + '\n\n[truncated]'.length)
    const exact = byId.get(ID_B)!
    assert.equal(exact.truncated, false)
    assert.equal(exact.body.length, FACT_MAX_BYTES)
  } finally {
    f.cleanup()
  }
})

test('deletes one fact and returns the refreshed list', () => {
  const f = fixture()
  try {
    const projectDir = factsDir(f.home, f.cwd, 'project')
    fs.writeFileSync(path.join(projectDir, `${ID_A}.md`), 'doomed')
    fs.writeFileSync(path.join(factsDir(f.home, f.cwd, 'global'), `${ID_B}.md`), 'keeper')
    const after = deleteFact(f.home, f.cwd, 'project', ID_A)
    assert.equal(fs.existsSync(path.join(projectDir, `${ID_A}.md`)), false)
    assert.equal(after.total, 1)
    assert.deepEqual(after.facts.map((x) => x.id), [ID_B])
  } finally {
    f.cleanup()
  }
})

test('deletes a hand-dropped fact the agent itself could not forget', () => {
  const f = fixture()
  try {
    const dir = factsDir(f.home, f.cwd, 'global')
    fs.writeFileSync(path.join(dir, 'notes.md'), 'hand dropped')
    const after = deleteFact(f.home, f.cwd, 'global', 'notes')
    assert.equal(fs.existsSync(path.join(dir, 'notes.md')), false)
    assert.deepEqual(after, { facts: [], total: 0 })
  } finally {
    f.cleanup()
  }
})

test('deleting an already-gone fact is not an error', () => {
  const f = fixture()
  try {
    assert.deepEqual(deleteFact(f.home, f.cwd, 'global', ID_A), { facts: [], total: 0 })
  } finally {
    f.cleanup()
  }
})

test('rejects a bad scope or an id that could leave the root', () => {
  const f = fixture()
  try {
    const keep = path.join(factsDir(f.home, f.cwd, 'global'), `${ID_A}.md`)
    fs.writeFileSync(keep, 'safe')
    assert.throws(() => deleteFact(f.home, f.cwd, 'elsewhere', ID_A), /Invalid memory scope/)
    assert.throws(() => deleteFact(f.home, f.cwd, 'global', '../secret'), /Invalid fact id/)
    assert.throws(() => deleteFact(f.home, f.cwd, 'global', 'a/b'), /Invalid fact id/)
    assert.throws(() => deleteFact(f.home, f.cwd, 'global', ''), /Invalid fact id/)
    assert.throws(() => deleteFact(f.home, '', 'project', ID_A), /No project directory/)
    assert.equal(fs.existsSync(keep), true)
  } finally {
    f.cleanup()
  }
})
