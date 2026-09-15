// Unit tests for the main-process skill discovery helpers (fs half; the pure
// rules live in desktop/tests/skill-rules.test.ts). Run from desktop/:
//   node --test "tests/**/*.test.ts"   (npm test)
//
// Node's built-in runner executes .ts directly (type stripping). The import
// carries the .ts extension because Node resolves it itself, which is why this
// file lives in tests/ rather than beside the module. The module under test IS
// typechecked (tsconfig.main.json includes src/main/**).
import { test } from 'node:test'
import assert from 'node:assert/strict'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'
import {
  isAllowedSkillPath,
  listSkills,
  readDisabledSlugs,
  scanSkillsRoot,
  setSkillDisabled,
  skillRoots,
} from '../src/main/skills.ts'

function fixture(): { home: string; cwd: string; cleanup: () => void } {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'cluxmate-skills-'))
  const home = path.join(base, 'home')
  const cwd = path.join(base, 'project')
  fs.mkdirSync(home, { recursive: true })
  fs.mkdirSync(cwd, { recursive: true })
  return { home, cwd, cleanup: () => fs.rmSync(base, { recursive: true, force: true }) }
}

// Write <root>/<slug>/SKILL.md, optionally with frontmatter name/description.
function writeSkill(root: string, slug: string, body = 'body', name?: string): string {
  const dir = path.join(root, slug)
  fs.mkdirSync(dir, { recursive: true })
  const fm = name ? `---\nname: ${name}\ndescription: desc of ${slug}\n---\n` : ''
  const file = path.join(dir, 'SKILL.md')
  fs.writeFileSync(file, fm + body)
  return file
}

function globalRoot(home: string): string {
  return path.join(home, '.cluxmate', 'skills')
}

function projectRoot(cwd: string): string {
  return path.join(cwd, '.cluxmate', 'skills')
}

function writeSkillsJson(cwd: string, cfg: unknown): string {
  const cfgPath = path.join(cwd, '.cluxmate', 'skills.json')
  fs.mkdirSync(path.dirname(cfgPath), { recursive: true })
  fs.writeFileSync(cfgPath, typeof cfg === 'string' ? cfg : JSON.stringify(cfg))
  return cfgPath
}

function readSkillsJson(cwd: string): any {
  return JSON.parse(fs.readFileSync(path.join(cwd, '.cluxmate', 'skills.json'), 'utf-8'))
}

test('scanSkillsRoot uses the directory name as the slug and id', () => {
  const f = fixture()
  try {
    const root = globalRoot(f.home)
    writeSkill(root, 'deploy', 'do it', 'Deploy Helper')
    fs.mkdirSync(path.join(root, 'not-a-skill'), { recursive: true })  // no SKILL.md
    const rows = scanSkillsRoot(root, 'global')
    assert.equal(rows.length, 1)
    assert.equal(rows[0].name, 'Deploy Helper')   // display label
    assert.equal(rows[0].slug, 'deploy')          // identity
    assert.equal(rows[0].id, 'global:deploy')     // disable key
    assert.equal(rows[0].source, 'global')
    assert.equal(rows[0].disabled, false)
    assert.equal(rows[0].shadowed, false)
  } finally {
    f.cleanup()
  }
})

test('missing root is empty, not an error', () => {
  const f = fixture()
  try {
    assert.deepEqual(scanSkillsRoot(path.join(f.home, 'nope'), 'global'), [])
    assert.deepEqual(listSkills(f.home, f.cwd), [])
  } finally {
    f.cleanup()
  }
})

test('roots are scanned farthest-first, global then project', () => {
  const roots = skillRoots('H', 'C')
  assert.deepEqual(roots.map((r) => r.source), ['global', 'project'])
  assert.equal(roots[0].root, path.join('H', '.cluxmate', 'skills'))
  assert.equal(roots[1].root, path.join('C', '.cluxmate', 'skills'))
})

// A bare entry (the original skills.json format) covers every root; a qualified
// one covers exactly one copy, which is what keeps a colliding slug toggleable.
test('scanSkillsRoot applies both entry forms from a legacy file', () => {
  const f = fixture()
  try {
    const root = globalRoot(f.home)
    writeSkill(root, 'bare')     // disabled by the bare entry
    writeSkill(root, 'qualified')
    writeSkill(root, 'untouched')
    const disabled = new Set(['bare', 'global:qualified'])
    const bySlug = new Map(scanSkillsRoot(root, 'global', disabled).map((s) => [s.slug, s]))
    assert.equal(bySlug.get('bare')?.disabled, true)
    assert.equal(bySlug.get('qualified')?.disabled, true)
    assert.equal(bySlug.get('untouched')?.disabled, false)
  } finally {
    f.cleanup()
  }
})

test('listSkills keeps both copies of a colliding slug, marking the loser', () => {
  const f = fixture()
  try {
    writeSkill(globalRoot(f.home), 'pdf', 'GLOBAL', 'Global PDF')
    writeSkill(projectRoot(f.cwd), 'pdf', 'PROJECT', 'Project PDF')
    writeSkill(globalRoot(f.home), 'solo', 'SOLO')

    const rows = listSkills(f.home, f.cwd)
    assert.equal(rows.length, 3)
    const byId = new Map(rows.map((r) => [r.id, r]))
    assert.equal(byId.get('global:pdf')?.shadowed, true)
    assert.equal(byId.get('global:pdf')?.name, 'Global PDF')
    assert.equal(byId.get('project:pdf')?.shadowed, false)
    assert.equal(byId.get('global:solo')?.shadowed, false)
  } finally {
    f.cleanup()
  }
})

test('listSkills reflects per-copy disable state', () => {
  const f = fixture()
  try {
    writeSkill(globalRoot(f.home), 'pdf')
    writeSkill(projectRoot(f.cwd), 'pdf')
    writeSkillsJson(f.cwd, { disabledSkills: ['project:pdf'] })

    const byId = new Map(listSkills(f.home, f.cwd).map((r) => [r.id, r]))
    assert.equal(byId.get('project:pdf')?.disabled, true)
    assert.equal(byId.get('global:pdf')?.disabled, false)
    // The global copy now serves the slug, so it is not a shadowed loser.
    assert.equal(byId.get('global:pdf')?.shadowed, false)
  } finally {
    f.cleanup()
  }
})

test('reads and writes the disable state, preserving other keys', () => {
  const f = fixture()
  try {
    writeSkill(globalRoot(f.home), 'legacy')  // the bare entry binds to this copy
    const cfgPath = writeSkillsJson(f.cwd, { disabledSkills: ['legacy'], otherKey: 1 })
    assert.deepEqual([...readDisabledSlugs(f.cwd)], ['legacy'])

    setSkillDisabled(f.home, f.cwd, 'global:legacy', true)
    // The bare entry is rewritten into the id it stood for, so the next toggle
    // can express a per-copy change; unrelated keys survive either way.
    assert.deepEqual(readSkillsJson(f.cwd).disabledSkills, ['global:legacy'])
    assert.equal(readSkillsJson(f.cwd).otherKey, 1)

    setSkillDisabled(f.home, f.cwd, 'global:legacy', false)
    assert.deepEqual(readDisabledSlugs(f.cwd), new Set())
    // Nothing left disabled drops the key entirely, leaving the file valid.
    assert.equal('disabledSkills' in readSkillsJson(f.cwd), false)
    assert.equal(readSkillsJson(f.cwd).otherKey, 1)
    assert.equal(fs.existsSync(cfgPath), true)
  } finally {
    f.cleanup()
  }
})

// The bug this guards: with a legacy bare entry in the file, toggling ONE copy
// back on used to write nothing that could express it (an absent id means
// enabled), so the row flipped back to disabled on the next list.
test('enabling one copy rewrites a legacy bare entry into per-copy ids', () => {
  const f = fixture()
  try {
    writeSkill(globalRoot(f.home), 'pdf')
    writeSkill(projectRoot(f.cwd), 'pdf')
    writeSkillsJson(f.cwd, { disabledSkills: ['pdf'], keep: 'me' })

    setSkillDisabled(f.home, f.cwd, 'project:pdf', false)

    assert.deepEqual(readSkillsJson(f.cwd).disabledSkills, ['global:pdf'])
    assert.equal(readSkillsJson(f.cwd).keep, 'me')
    const byId = new Map(listSkills(f.home, f.cwd).map((r) => [r.id, r]))
    assert.equal(byId.get('project:pdf')?.disabled, false)  // the toggle stuck
    assert.equal(byId.get('global:pdf')?.disabled, true)    // the other copy stayed off
    assert.equal(byId.get('global:pdf')?.shadowed, false)
  } finally {
    f.cleanup()
  }
})

test('a bare entry for a slug that is not installed is left alone', () => {
  const f = fixture()
  try {
    writeSkill(globalRoot(f.home), 'pdf')
    writeSkillsJson(f.cwd, { disabledSkills: ['ghost'] })
    setSkillDisabled(f.home, f.cwd, 'global:pdf', true)
    assert.deepEqual(readDisabledSlugs(f.cwd), new Set(['ghost', 'global:pdf']))
  } finally {
    f.cleanup()
  }
})

test('a missing or malformed skills.json disables nothing', () => {
  const f = fixture()
  try {
    assert.deepEqual(readDisabledSlugs(f.cwd), new Set())
    // Not JSON, and valid JSON that is not an object: none of these may throw.
    for (const body of ['{not json', '[]', 'null', '"pdf"', '3', '{"disabledSkills": 7}']) {
      writeSkillsJson(f.cwd, body)
      assert.deepEqual(readDisabledSlugs(f.cwd), new Set())
    }
    assert.deepEqual(listSkills(f.home, f.cwd), [])
    // Writing over a malformed file still produces a valid one.
    writeSkill(globalRoot(f.home), 'pdf')
    writeSkillsJson(f.cwd, '[]')
    setSkillDisabled(f.home, f.cwd, 'global:pdf', true)
    assert.deepEqual([...readDisabledSlugs(f.cwd)], ['global:pdf'])
    assert.equal(Array.isArray(readSkillsJson(f.cwd)), false)
  } finally {
    f.cleanup()
  }
})

test('rejects an id that could reach outside the skills roots', () => {
  const f = fixture()
  try {
    // A write always carries a qualified id; the bare form (read-only, "every
    // root") and anything with a separator or an unknown source is refused.
    for (const bad of ['../escape', 'a/b', 'user:pdf', 'pdf', 'global:../escape', 'global:a\\b', '']) {
      assert.throws(() => setSkillDisabled(f.home, f.cwd, bad, true), /Invalid skill id/)
    }
    assert.equal(fs.existsSync(path.join(f.cwd, '.cluxmate', 'skills.json')), false)
  } finally {
    f.cleanup()
  }
})

// A skill directory may legitimately be named "My Skill": the id is a JSON
// array entry compared by string equality, never a path component, so such a
// slug must be toggleable rather than wedging the row.
test('a slug with a space or unicode is toggleable', () => {
  const f = fixture()
  try {
    writeSkill(globalRoot(f.home), 'My Skill')
    setSkillDisabled(f.home, f.cwd, 'global:My Skill', true)
    assert.deepEqual([...readDisabledSlugs(f.cwd)], ['global:My Skill'])
    assert.equal(listSkills(f.home, f.cwd)[0].disabled, true)
    setSkillDisabled(f.home, f.cwd, 'global:My Skill', false)
    assert.deepEqual(readDisabledSlugs(f.cwd), new Set())
  } finally {
    f.cleanup()
  }
})

// The read handler serves SKILL.md files only from the two known roots.
test('isAllowedSkillPath accepts both roots and rejects traversal', () => {
  const f = fixture()
  try {
    const globalMd = writeSkill(globalRoot(f.home), 'deploy')
    const projectMd = writeSkill(projectRoot(f.cwd), 'deploy')
    assert.equal(isAllowedSkillPath(globalMd, f.home, f.cwd), true)
    assert.equal(isAllowedSkillPath(projectMd, f.home, f.cwd), true)

    // Right file name, wrong place.
    const stray = path.join(f.cwd, 'deploy', 'SKILL.md')
    fs.mkdirSync(path.dirname(stray), { recursive: true })
    fs.writeFileSync(stray, 'x')
    assert.equal(isAllowedSkillPath(stray, f.home, f.cwd), false)

    // Right place, wrong file name.
    const other = path.join(globalRoot(f.home), 'deploy', 'README.md')
    fs.writeFileSync(other, 'x')
    assert.equal(isAllowedSkillPath(other, f.home, f.cwd), false)

    assert.equal(
      isAllowedSkillPath(path.join(globalRoot(f.home), '..', 'config.json'), f.home, f.cwd),
      false,
    )
  } finally {
    f.cleanup()
  }
})
