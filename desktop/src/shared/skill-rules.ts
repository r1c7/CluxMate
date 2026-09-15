// Skill identity + collision rules, shared by the main process (list/toggle IPC
// in src/main/skills.ts) and the renderer (the optimistic list updates in
// renderer/stores). Living in one place is what keeps the row the UI shows and
// the row the main process reports from disagreeing: both call markShadowed().
//
// Semantics — mirrored by the agent side (cluxmate/core/skills.py):
//   * a skill's identity is its directory name (the **slug**); the frontmatter
//     `name` is only a display label;
//   * `id` is source-qualified — "<source>:<slug>" — and is the disable key, so
//     the two copies of a colliding slug toggle independently;
//   * roots are ranked farthest → nearest (SKILL_SOURCES): a slug installed in
//     more than one root is served by the nearest ENABLED copy, and the other
//     enabled copies are marked `shadowed`;
//   * a disabled copy is never shadowed — it is merely off, and a disabled
//     nearer copy shadows nothing. That is what lets disabling the project copy
//     fall back to the global one.
//
// Pure data logic, no fs/electron: importable from both bundles and directly
// testable (desktop/tests/skill-rules.test.ts).

export type SkillSource = 'global' | 'project'

// Farthest → nearest. This order IS the precedence rule (see skillRank), so
// main/skills.ts derives its scan order from it rather than repeating it.
export const SKILL_SOURCES: readonly SkillSource[] = ['global', 'project']

// The fields the rules need; SkillMeta (shared/types.ts) extends this.
export interface SkillIdentity {
  slug: string
  source: SkillSource
  disabled: boolean
}

export function skillId(source: SkillSource, slug: string): string {
  return `${source}:${slug}`
}

export function skillRank(source: SkillSource): number {
  return SKILL_SOURCES.indexOf(source)
}

// Parse a source-qualified id — the form every WRITE carries (the UI always has
// one). A bare slug is only ever read: it means "every root" (the format an
// existing skills.json holds, see isSkillDisabled).
//
// The slug is a directory name, so anything but a path separator, a second
// colon (which would make the id ambiguous) and a degenerate "."/".." is
// allowed — a skill dir may legitimately be named "My Skill". The id is only
// ever a JSON array entry compared by string equality, never a path component.
export function parseSkillId(id: string): { source: SkillSource; slug: string } | null {
  const m = /^(global|project):([^/\\:]+)$/.exec(id)
  if (!m) return null
  const slug = m[2]
  if (slug === '.' || slug === '..' || slug !== slug.trim()) return null
  return { source: m[1] as SkillSource, slug }
}

// A bare slug disables every root; a qualified id disables that one copy.
export function isSkillDisabled(disabledSlugs: Set<string>, source: SkillSource, slug: string): boolean {
  return disabledSlugs.has(slug) || disabledSlugs.has(skillId(source, slug))
}

// Flag the copies a nearer enabled copy overrides. Order-independent — the rank
// decides, not the array position — so callers may pass a display-sorted list.
// Both rows stay in the result: an overridden copy is listed and toggleable.
export function markShadowed<T extends SkillIdentity>(skills: T[]): (T & { shadowed: boolean })[] {
  const winner = new Map<string, number>()
  for (const s of skills) {
    if (s.disabled) continue
    const rank = skillRank(s.source)
    if (!winner.has(s.slug) || rank > winner.get(s.slug)!) winner.set(s.slug, rank)
  }
  return skills.map((s) => ({
    ...s,
    shadowed: !s.disabled && winner.get(s.slug) !== skillRank(s.source),
  }))
}
