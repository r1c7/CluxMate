// Skill discovery for the desktop's skills view. This is the read-only mirror
// of the agent-side loader (cluxmate/core/skills.py); the identity/collision
// rules both sides obey live in ../shared/skill-rules.ts, and this module adds
// the filesystem half: scanning the two roots and reading/writing the disable
// state.
//
// Pure filesystem + string logic (no electron imports) so desktop/tests can
// exercise it directly with the Node test runner.

import * as fs from 'fs'
import * as path from 'path'
import type { SkillMeta } from '../shared/types'
// The .ts extension is deliberate: this module is loaded directly by the Node
// test runner (desktop/tests/skills.test.ts), which resolves paths itself and
// rejects an extensionless relative import. tsconfig.main.json enables
// allowImportingTsExtensions for exactly this.
import {
  SKILL_SOURCES,
  isSkillDisabled,
  markShadowed,
  parseSkillId,
  skillId,
  type SkillSource,
} from '../shared/skill-rules.ts'

export const SKILL_MAX_BYTES = 256 * 1024

// Frontmatter is a leading `---\n ... \n---` block. Only name/description are
// read; values may be quoted. Anything else is ignored.
export function parseFrontmatter(md: string): { name?: string; description?: string } {
  if (!md.startsWith('---')) return {}
  const end = md.indexOf('\n---', 3)
  if (end === -1) return {}
  const block = md.slice(3, end)
  const out: { name?: string; description?: string } = {}
  for (const line of block.split('\n')) {
    const m = /^\s*(name|description)\s*:\s*(.*)$/.exec(line)
    if (m) {
      let v = m[2].trim()
      if ((v.startsWith('"') && v.endsWith('"')) || (v.startsWith("'") && v.endsWith("'"))) {
        v = v.slice(1, -1)
      }
      out[m[1] as 'name' | 'description'] = v
    }
  }
  return out
}

// Scan roots farthest-first, mirroring Python's SkillManager._roots(): the
// later entry is the nearer one and wins a slug collision.
export function skillRoots(home: string, cwd: string): { root: string; source: SkillSource }[] {
  return SKILL_SOURCES.map((source) => ({
    root: path.join(source === 'global' ? home : cwd, '.cluxmate', 'skills'),
    source,
  }))
}

// Raw ids from <cwd>/.cluxmate/skills.json → disabledSkills, kept verbatim;
// which copies each one covers is decided by isSkillDisabled. A file that is
// missing, unreadable, not JSON, or not a JSON object disables nothing — the
// agent-side twin (core/skills.py _read_disabled_slugs) behaves identically.
export function readDisabledSlugs(cwd: string): Set<string> {
  const cfgPath = path.join(cwd, '.cluxmate', 'skills.json')
  try {
    const cfg = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'))
    if (cfg && !Array.isArray(cfg) && typeof cfg === 'object' && Array.isArray(cfg.disabledSkills)) {
      return new Set(cfg.disabledSkills.filter((s: unknown): s is string => typeof s === 'string'))
    }
  } catch {}
  return new Set()
}

// A bare "pdf" entry means "every copy of pdf" — the older file format, still
// accepted when reading. A per-copy toggle cannot be expressed while such an
// entry stands (an absent id means enabled, so the bare entry would keep
// winning), so every write first rewrites the bare entries it can bind to a
// copy into explicit per-copy ids: the meaning is preserved exactly — each copy
// that was off stays off — and the toggle then lands on a file that can
// represent it. A bare entry naming a slug we cannot see here is left as is.
function expandBareEntries(list: string[], home: string, cwd: string): string[] {
  const copies = new Map<string, string[]>()
  for (const { root, source } of skillRoots(home, cwd)) {
    for (const sk of scanSkillsRoot(root, source)) {
      copies.set(sk.slug, [...(copies.get(sk.slug) ?? []), sk.id])
    }
  }
  const out: string[] = []
  const push = (v: string) => { if (!out.includes(v)) out.push(v) }
  for (const entry of list) {
    const bound = entry.includes(':') ? undefined : copies.get(entry)
    if (bound) bound.forEach(push)
    else push(entry)
  }
  return out
}

// Add/remove one copy's id in <cwd>/.cluxmate/skills.json, so the two copies of
// a colliding slug toggle independently. Other keys are preserved; the
// disabledSkills key disappears again once nothing is disabled.
export function setSkillDisabled(home: string, cwd: string, id: string, disabled: boolean): void {
  if (!parseSkillId(id)) throw new Error('Invalid skill id')
  const cfgPath = path.join(cwd, '.cluxmate', 'skills.json')
  let cfg: Record<string, any> = {}
  try {
    const parsed = JSON.parse(fs.readFileSync(cfgPath, 'utf-8'))
    if (parsed && !Array.isArray(parsed) && typeof parsed === 'object') cfg = parsed
  } catch {}
  let list: string[] = Array.isArray(cfg.disabledSkills)
    ? expandBareEntries(
        cfg.disabledSkills.filter((s: unknown): s is string => typeof s === 'string'),
        home,
        cwd,
      )
    : []
  if (disabled) {
    if (!list.includes(id)) list.push(id)
  } else {
    list = list.filter((s: string) => s !== id)
  }
  cfg.disabledSkills = list
  if (list.length === 0) delete cfg.disabledSkills
  fs.mkdirSync(path.dirname(cfgPath), { recursive: true })
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2), 'utf-8')
}

export function scanSkillsRoot(
  root: string,
  source: SkillSource,
  disabledSlugs?: Set<string>,
): SkillMeta[] {
  const out: SkillMeta[] = []
  let entries: fs.Dirent[]
  try {
    entries = fs.readdirSync(root, { withFileTypes: true })
  } catch {
    return out // root doesn't exist — fine, just no skills there
  }
  for (const e of entries) {
    if (!e.isDirectory()) continue
    const skillMd = path.join(root, e.name, 'SKILL.md')
    if (!fs.existsSync(skillMd)) continue
    let fm: { name?: string; description?: string } = {}
    try {
      fm = parseFrontmatter(fs.readFileSync(skillMd, 'utf-8').slice(0, 4096))
    } catch { /* unreadable — still list it by dir name */ }
    out.push({
      name: fm.name || e.name,
      description: fm.description || '',
      slug: e.name,
      id: skillId(source, e.name),
      source,
      path: skillMd,
      disabled: disabledSlugs ? isSkillDisabled(disabledSlugs, source, e.name) : false,
      shadowed: false, // filled in by markShadowed once every root is known
    })
  }
  return out
}

// Every installed skill across both roots, shadowing flagged. Both copies of a
// colliding slug are returned.
export function listSkills(home: string, cwd: string): SkillMeta[] {
  const disabled = readDisabledSlugs(cwd)
  const all = skillRoots(home, cwd).flatMap((r) => scanSkillsRoot(r.root, r.source, disabled))
  return markShadowed(all)
}

// A path is a legitimate skill file only if it's a SKILL.md directly inside a
// subdirectory of one of the known roots. Guards the read handler against
// path-traversal (e.g. a crafted "../../secret").
export function isAllowedSkillPath(p: string, home: string, cwd: string): boolean {
  const resolved = path.resolve(p)
  if (path.basename(resolved) !== 'SKILL.md') return false
  const parent = path.dirname(path.dirname(resolved)) // <root>/<skill>/SKILL.md -> <root>
  return skillRoots(home, cwd).some((r) => path.resolve(r.root) === parent)
}
