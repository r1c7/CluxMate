// Unit tests for the shared skill identity/collision rules (imported by both
// the main process and the renderer). Run from desktop/:  npm test
//
// Node's built-in runner executes .ts directly (type stripping). The import
// carries the .ts extension because Node resolves it itself, which is why this
// file lives in tests/ rather than beside the module.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  SKILL_SOURCES,
  isSkillDisabled,
  markShadowed,
  parseSkillId,
  skillId,
  skillRank,
  type SkillIdentity,
} from '../src/shared/skill-rules.ts'

function row(source: 'global' | 'project', slug: string, disabled = false): SkillIdentity {
  return { slug, source, disabled }
}

test('the root order is the precedence rule, farthest first', () => {
  assert.deepEqual([...SKILL_SOURCES], ['global', 'project'])
  assert.ok(skillRank('project') > skillRank('global'))
})

test('skillId qualifies the slug with its source', () => {
  assert.equal(skillId('global', 'pdf'), 'global:pdf')
  assert.equal(skillId('project', 'pdf'), 'project:pdf')
})

// Writes always carry a qualified id (the UI has one); a bare slug is only ever
// READ, where it means "every root".
test('parseSkillId accepts the qualified form only', () => {
  assert.deepEqual(parseSkillId('global:pdf'), { source: 'global', slug: 'pdf' })
  assert.deepEqual(parseSkillId('project:my-skill_2'), { source: 'project', slug: 'my-skill_2' })
  // A slug is a directory name: spaces and unicode are fine (the id is a JSON
  // array entry, never a path), separators and stray colons are not.
  assert.deepEqual(parseSkillId('global:My Skill'), { source: 'global', slug: 'My Skill' })
  assert.deepEqual(parseSkillId('global:技能'), { source: 'global', slug: '技能' })
  assert.equal(parseSkillId('pdf'), null)
  assert.equal(parseSkillId('user:pdf'), null)
  assert.equal(parseSkillId('global:../escape'), null)
  assert.equal(parseSkillId('global:a/b'), null)
  assert.equal(parseSkillId('global:a\\b'), null)
  assert.equal(parseSkillId('global:a:b'), null)
  assert.equal(parseSkillId('global:.'), null)
  assert.equal(parseSkillId('global:..'), null)
  assert.equal(parseSkillId('global: pdf '), null)
  assert.equal(parseSkillId('global:'), null)
  assert.equal(parseSkillId(''), null)
})

test('a bare slug disables every copy and a qualified id only one', () => {
  assert.equal(isSkillDisabled(new Set(['deploy']), 'global', 'deploy'), true)
  assert.equal(isSkillDisabled(new Set(['deploy']), 'project', 'deploy'), true)
  assert.equal(isSkillDisabled(new Set(['global:deploy']), 'global', 'deploy'), true)
  assert.equal(isSkillDisabled(new Set(['global:deploy']), 'project', 'deploy'), false)
  assert.equal(isSkillDisabled(new Set(['project:deploy']), 'global', 'deploy'), false)
  assert.equal(isSkillDisabled(new Set(['deploy-other']), 'global', 'deploy'), false)
  assert.equal(isSkillDisabled(new Set(), 'global', 'deploy'), false)
})

test('the nearer root overrides and flags the farther copy', () => {
  const rows = markShadowed([row('global', 'pdf'), row('project', 'pdf'), row('global', 'solo')])
  const bySlug = new Map(rows.map((r) => [`${r.source}:${r.slug}`, r.shadowed]))
  assert.equal(bySlug.get('global:pdf'), true)   // overridden
  assert.equal(bySlug.get('project:pdf'), false) // the winner
  assert.equal(bySlug.get('global:solo'), false) // no project copy
})

// The rank decides, not the array position — the renderer passes a
// display-sorted list (global rows first today), and a reorder must not change
// which row wears the marker.
test('markShadowed is order-independent', () => {
  const projectFirst = markShadowed([row('project', 'pdf'), row('global', 'pdf')])
  const globalFirst = markShadowed([row('global', 'pdf'), row('project', 'pdf')])
  assert.deepEqual(projectFirst.map((r) => r.shadowed), [false, true])
  assert.deepEqual(globalFirst.map((r) => r.shadowed), [true, false])
})

// A disabled copy is merely off: it neither shadows nor is shadowed, which is
// what lets disabling the project copy fall back to the global one.
test('disabled copies are never flagged as shadowed', () => {
  const nearerOff = markShadowed([row('global', 'pdf'), row('project', 'pdf', true)])
  assert.deepEqual(nearerOff.map((r) => r.shadowed), [false, false])

  const fartherOff = markShadowed([row('global', 'pdf', true), row('project', 'pdf')])
  assert.deepEqual(fartherOff.map((r) => r.shadowed), [false, false])

  const bothOff = markShadowed([row('global', 'pdf', true), row('project', 'pdf', true)])
  assert.deepEqual(bothOff.map((r) => r.shadowed), [false, false])
})

test('markShadowed passes unrelated rows through untouched', () => {
  const rows = markShadowed([{ ...row('global', 'a'), name: 'A', path: '/x' } as any])
  assert.equal(rows[0].name, 'A')
  assert.equal(rows[0].path, '/x')
  assert.equal(rows[0].shadowed, false)
  assert.equal(rows.length, 1)
})
