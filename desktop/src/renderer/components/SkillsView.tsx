import React, { useEffect, useState } from 'react'
import { useStore } from '../stores'
import type { SkillMeta } from '../../shared/types'
import { TextChunk } from './MessageBubble'
import { useT } from '../useI18n'

const SOURCE_BADGE: Record<SkillMeta['source'], { labelKey: string; cls: string }> = {
  global: { labelKey: 'skills.sourceGlobal', cls: 'bg-accent/15 text-accent border-accent/30' },
  project: { labelKey: 'skills.sourceProject', cls: 'bg-emerald-500/10 text-emerald-700 border-emerald-500/30' },
}

// A SKILL.md is YAML frontmatter (--- ... ---) followed by the markdown body.
// Feeding the whole thing to a markdown renderer turns the `---` fences into
// horizontal rules and the `key: value` lines into stray text, so we split the
// two: frontmatter becomes a structured meta table, the body renders as
// markdown. Frontmatter keys are shown verbatim (order preserved); only simple
// `key: value` lines are parsed — anything fancier stays as a raw line.
interface ParsedSkill {
  meta: { key: string; value: string }[]
  body: string
}

function parseSkill(raw: string): ParsedSkill {
  if (!raw.startsWith('---')) return { meta: [], body: raw }
  // Find the closing fence: a line that is exactly `---` after the opening one.
  const end = raw.indexOf('\n---', 3)
  if (end === -1) return { meta: [], body: raw }
  const block = raw.slice(3, end).replace(/^\n/, '')
  // Body starts after the closing `---` line.
  const afterFence = raw.indexOf('\n', end + 1)
  const body = afterFence === -1 ? '' : raw.slice(afterFence + 1).replace(/^\s*\n/, '')

  const meta: { key: string; value: string }[] = []
  for (const line of block.split('\n')) {
    const m = /^([A-Za-z0-9_-]+)\s*:\s*(.*)$/.exec(line)
    if (m) {
      let v = m[2].trim()
      if (v.length >= 2 && (v[0] === '"' || v[0] === "'") && v[v.length - 1] === v[0]) {
        v = v.slice(1, -1)
      }
      meta.push({ key: m[1], value: v })
    } else if (line.trim()) {
      // Non key:value frontmatter line (list item, folded scalar, …) — keep it
      // readable rather than dropping it.
      meta.push({ key: '', value: line.trim() })
    }
  }
  return { meta, body }
}

// Main-area view (swaps in for ChatView) that lists installed skills on the
// left and previews the selected skill's SKILL.md on the right. Read-only.
export default function SkillsView() {
  const t = useT()
  const skills = useStore((s) => s.skills)
  const selectedPath = useStore((s) => s.selectedSkillPath)
  const skillContent = useStore((s) => s.skillContent)
  const selectSkill = useStore((s) => s.selectSkill)
  const showSkills = useStore((s) => s.showSkills)
  const setSkillDisabled = useStore((s) => s.setSkillDisabled)
  const activeSessionId = useStore((s) => s.activeSessionId)

  // Slugs toggled this view — persist is written to skills.json but the
  // running agent won't hot-swap its system prompt, so flag pending restart.
  const [pendingRestart, setPendingRestart] = useState<Set<string>>(new Set())

  // Re-scan when the active session (hence project cwd) changes.
  useEffect(() => {
    showSkills()
    setPendingRestart(new Set())
  }, [activeSessionId])

  const onToggle = (slug: string, disabled: boolean) => {
    setSkillDisabled(slug, disabled)
    setPendingRestart((prev) => new Set(prev).add(slug))
  }

  // Extract slug from a skill's path: <root>/<slug>/SKILL.md → slug.
  function getSlug(p: string): string {
    const parts = p.replace(/\\/g, '/').split('/')
    return parts[parts.length - 2] || ''
  }

  const selected = skills.find((s) => s.path === selectedPath)

  return (
    <div className="flex-1 flex min-h-0">
      {/* Skill list */}
      <div className="w-64 flex-shrink-0 border-r border-surface-border flex flex-col">
        <div className="px-3 h-9 flex items-center border-b border-surface-border flex-shrink-0">
          <span className="text-xs font-semibold text-ink-soft">{t('skills.installed')}</span>
          <span className="text-[10px] text-ink-faint ml-auto">{skills.length}</span>
        </div>
        <div className="flex-1 overflow-y-auto py-1">
          {skills.length === 0 ? (
            <p className="text-xs text-ink-faint px-3 py-3 leading-relaxed">
              {t('skills.none')}
            </p>
          ) : (
            skills.map((sk) => {
              const badge = SOURCE_BADGE[sk.source]
              const isSel = sk.path === selectedPath
              const slug = getSlug(sk.path)
              return (
                <div
                  key={sk.path}
                  onClick={() => selectSkill(sk.path)}
                  className={`w-full text-left px-3 py-2 border-l-2 transition-colors cursor-pointer ${
                    isSel
                      ? 'bg-sidebar-active border-accent'
                      : 'border-transparent hover:bg-sidebar-hover'
                  }`}
                  title={sk.path}
                >
                  <div className="flex items-center gap-1.5">
                    <span className={`text-sm truncate flex-1 min-w-0 ${sk.disabled ? 'text-ink-faint' : 'text-ink'}`}>{sk.name}</span>
                    <span className={`text-[9px] px-1 py-0.5 rounded border flex-shrink-0 ${badge.cls}`}>
                      {t(badge.labelKey)}
                    </span>
                    {/* Disable toggle — writes to project skills.json, takes
                        effect next session (no hot-swap). Optimistic. */}
                    <button
                      onClick={(e) => {
                        e.stopPropagation()
                        onToggle(slug, !sk.disabled)
                      }}
                      title={sk.disabled ? t('skills.toggleDisabledTitle') : t('skills.toggleEnabledTitle')}
                      className={`relative w-9 h-5 rounded-full transition-colors flex-shrink-0 hover:opacity-80 ${
                        sk.disabled ? 'bg-surface-border' : 'bg-accent'
                      }`}
                    >
                      <span
                        className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-all ${
                          sk.disabled ? 'left-0.5' : 'left-[18px]'
                        }`}
                      />
                    </button>
                  </div>
                  {sk.description && (
                    <div className={`text-xs truncate mt-0.5 ${sk.disabled ? 'text-ink-faint/60' : 'text-ink-faint'}`}>{sk.description}</div>
                  )}
                  {pendingRestart.has(slug) && (
                    <div className="text-xs text-amber-600 mt-0.5">{t('skills.restartNote')}</div>
                  )}
                </div>
              )
            })
          )}
        </div>
      </div>

      {/* SKILL.md preview — frontmatter as a meta table, body as markdown. */}
      <div className="flex-1 flex flex-col min-w-0">
        {selected ? (
          <>
            <div className="px-4 h-9 flex items-center gap-2 border-b border-surface-border flex-shrink-0">
              <span className="text-xs font-mono text-ink-soft truncate">{selected.name}</span>
              <span className="text-[10px] text-ink-faint truncate">{selected.path}</span>
            </div>
            <SkillBody raw={skillContent} />
          </>
        ) : (
          <div className="flex-1 flex items-center justify-center text-ink-faint text-sm">
            {t('skills.selectHint')}
          </div>
        )}
      </div>
    </div>
  )
}

// Splits a SKILL.md into a metadata card (frontmatter) and a markdown body.
function SkillBody({ raw }: { raw: string }) {
  const t = useT()
  const { meta, body } = parseSkill(raw)
  return (
    <div className="flex-1 overflow-y-auto px-6 py-4 select-text">
      {meta.length > 0 && (
        <div className="mb-4 rounded-md border border-surface-border bg-surface-raised/40 overflow-hidden">
          <div className="px-3 py-1.5 border-b border-surface-border">
            <span className="text-[10px] uppercase tracking-wide text-ink-faint/70">{t('skills.metadata')}</span>
          </div>
          <dl className="divide-y divide-surface-border/50">
            {meta.map((m, i) => (
              <div key={i} className="flex gap-3 px-3 py-1.5">
                {m.key ? (
                  <>
                    <dt className="text-xs font-mono text-ink-faint w-32 flex-shrink-0">{m.key}</dt>
                    <dd className="text-xs text-ink-soft break-words min-w-0">{m.value}</dd>
                  </>
                ) : (
                  <dd className="text-xs text-ink-faint italic break-words">{m.value}</dd>
                )}
              </div>
            ))}
          </dl>
        </div>
      )}
      {body.trim()
        ? <TextChunk text={body} />
        : <p className="text-xs text-ink-faint italic">{t('skills.noBody')}</p>}
    </div>
  )
}
