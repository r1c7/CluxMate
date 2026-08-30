import React, { useEffect, useState } from 'react'
import { useStore } from '../stores'
import { THEMES } from '../themes'
import { FONT_OPTIONS } from '../fonts'
import { useT } from '../useI18n'
import type { MessageKey } from '../i18n'
import { reasoningValuesFor } from '../../shared/reasoning'
import type { ModelEntry } from '../../shared/types'

type Section = 'model' | 'theme' | 'font' | 'sandbox' | 'language'

const SECTIONS: { id: Section; labelKey: MessageKey; icon: React.ReactNode }[] = [
  {
    id: 'model', labelKey: 'settings.section.model',
    icon: (
      <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <rect x="4" y="4" width="16" height="16" rx="2" />
        <rect x="9" y="9" width="6" height="6" />
        <path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2" />
      </svg>
    ),
  },
  {
    id: 'theme', labelKey: 'settings.section.theme',
    icon: (
      <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="13.5" cy="6.5" r=".5" fill="currentColor" />
        <circle cx="17.5" cy="10.5" r=".5" fill="currentColor" />
        <circle cx="8.5" cy="7.5" r=".5" fill="currentColor" />
        <circle cx="6.5" cy="12.5" r=".5" fill="currentColor" />
        <path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z" />
      </svg>
    ),
  },
  {
    id: 'font', labelKey: 'settings.section.font',
    icon: (
      <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <polyline points="4 7 4 4 20 4 20 7" />
        <line x1="9" y1="20" x2="15" y2="20" />
        <line x1="12" y1="4" x2="12" y2="20" />
      </svg>
    ),
  },
  {
    id: 'sandbox', labelKey: 'settings.section.sandbox',
    icon: (
      <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
        <path d="M9 12l2 2 4-4" />
      </svg>
    ),
  },
  {
    id: 'language', labelKey: 'settings.section.language',
    icon: (
      <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <circle cx="12" cy="12" r="10" />
        <path d="M2 12h20" />
        <path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z" />
      </svg>
    ),
  },
]

// Built-in denied networks (mirror of cluxmate/tools/_ssrf.py::DEFAULT_BLOCKED_NETS) —
// displayed read-only; users can only ADD to block_extra, never remove these.
const DEFAULT_BLOCKED_NETS = [
  '0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
  '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16',
  '198.18.0.0/15', '224.0.0.0/4', '240.0.0.0/4',
  '::/128', '::1/128', 'fc00::/7', 'fe80::/10', 'ff00::/8', '64:ff9b::/96',
]

function newId(): string {
  return 'm_' + Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 6)
}

function blankEntry(): ModelEntry {
  return {
    id: newId(), api_type: 'openai', provider: 'New Model',
    base_url: '', api_key: '', model_name: '', context_1m: false,
    max_tokens: 0,
  }
}

// Full-page settings view (swaps in for ChatView) — the model / theme / font /
// language sections live in a left sidebar, their content in the right pane.
// Mirrors the SkillsView / McpView two-pane layout instead of the old centered
// modal.
export default function SettingsView() {
  const t = useT()
  const models = useStore((s) => s.models)
  const defaultModelId = useStore((s) => s.defaultModelId)
  const initConfig = useStore((s) => s.initConfig)
  const showChat = useStore((s) => s.showChat)
  const theme = useStore((s) => s.theme)
  const setTheme = useStore((s) => s.setTheme)
  const fontFamily = useStore((s) => s.fontFamily)
  const fontSize = useStore((s) => s.fontSize)
  const setFontFamily = useStore((s) => s.setFontFamily)
  const setFontSize = useStore((s) => s.setFontSize)
  const lang = useStore((s) => s.lang)
  const setLanguage = useStore((s) => s.setLanguage)

  const [section, setSection] = useState<Section>('model')

  // Local working copy — committed to config.json only on Save. Initialized from
  // the store on mount (this view unmounts when the user leaves, so the draft is
  // never stale from a previous visit).
  const [draft, setDraft] = useState<ModelEntry[]>(() => models.map((m) => ({ ...m })))
  const [activeId, setActiveId] = useState(defaultModelId)
  const [editingId, setEditingId] = useState<string | null>(null)

  // Sandbox writable-folder grants — user-global, read once on mount and
  // committed immediately on add/remove (each change is a full replace that
  // reconciles revocations on the Python/main side).
  const [grants, setGrants] = useState<string[]>([])
  const [grantsLoaded, setGrantsLoaded] = useState(false)
  useEffect(() => {
    let alive = true
    window.electronAPI.getSandboxGrants().then((r) => {
      if (alive) { setGrants(r.paths); setGrantsLoaded(true) }
    }).catch(() => { if (alive) setGrantsLoaded(true) })
    return () => { alive = false }
  }, [])

  const commitGrants = async (next: string[]) => {
    // Optimistic update; the backend reply carries the canonical list.
    setGrants(next)
    try {
      const r = await window.electronAPI.setSandboxGrants(next)
      setGrants(r.paths)
    } catch { /* keep the optimistic list on failure */ }
  }

  const addGrant = async () => {
    const dir = await window.electronAPI.selectDirectory()
    if (!dir) return
    if (!grants.includes(dir)) await commitGrants([...grants, dir])
  }

  const removeGrant = async (p: string) => {
    await commitGrants(grants.filter((g) => g !== p))
  }

  // Sandbox read-denylist (forbid-read.json) — user-global, mirroring grants:
  // read once on mount, committed immediately on add/remove (full replace).
  const [forbidRead, setForbidRead] = useState<string[]>([])
  const [forbidReadLoaded, setForbidReadLoaded] = useState(false)
  useEffect(() => {
    let alive = true
    window.electronAPI.getForbidRead().then((r) => {
      if (alive) { setForbidRead(r.paths); setForbidReadLoaded(true) }
    }).catch(() => { if (alive) setForbidReadLoaded(true) })
    return () => { alive = false }
  }, [])

  const commitForbidRead = async (next: string[]) => {
    setForbidRead(next)
    try {
      const r = await window.electronAPI.setForbidRead(next)
      setForbidRead(r.paths)
    } catch { /* keep the optimistic list on failure */ }
  }

  const addForbidRead = async () => {
    const dir = await window.electronAPI.selectDirectory()
    if (!dir) return
    if (!forbidRead.includes(dir)) await commitForbidRead([...forbidRead, dir])
  }

  const removeForbidRead = async (p: string) => {
    await commitForbidRead(forbidRead.filter((f) => f !== p))
  }

  // SSRF network-access config (ssrf.json) — user-global, mirroring grants.
  const [ssrfAllow, setSsrAllow] = useState<string[]>([])
  const [ssrfBlockExtra, setSsrBlockExtra] = useState<string[]>([])
  const [ssrfLoaded, setSsrLoaded] = useState(false)
  const [ssrfAllowInput, setSsrAllowInput] = useState('')
  const [ssrfBlockInput, setSsrBlockInput] = useState('')

  useEffect(() => {
    let alive = true
    window.electronAPI.getSsrConfig().then((r) => {
      if (alive) { setSsrAllow(r.allow); setSsrBlockExtra(r.block_extra); setSsrLoaded(true) }
    }).catch(() => { if (alive) setSsrLoaded(true) })
    return () => { alive = false }
  }, [])

  const commitSsr = async (allow: string[], blockExtra: string[]) => {
    setSsrAllow(allow); setSsrBlockExtra(blockExtra)
    try {
      const r = await window.electronAPI.setSsrConfig({ allow, block_extra: blockExtra })
      setSsrAllow(r.allow); setSsrBlockExtra(r.block_extra)
    } catch { /* keep the optimistic list on failure */ }
  }

  const addSsrAllow = () => {
    const v = ssrfAllowInput.trim()
    if (!v) return
    if (!ssrfAllow.includes(v)) commitSsr([...ssrfAllow, v], ssrfBlockExtra)
    setSsrAllowInput('')
  }

  const addSsrBlockExtra = () => {
    const v = ssrfBlockInput.trim()
    if (!v) return
    if (!ssrfBlockExtra.includes(v)) commitSsr(ssrfAllow, [...ssrfBlockExtra, v])
    setSsrBlockInput('')
  }

  const patch = (id: string, fields: Partial<ModelEntry>) =>
    setDraft((d) => d.map((m) => (m.id === id ? { ...m, ...fields } : m)))

  const addModel = () => {
    const e = blankEntry()
    setDraft((d) => [...d, e])
    setActiveId((a) => a || e.id)
    setEditingId(e.id)
  }

  const deleteModel = (id: string) => {
    setDraft((d) => {
      const next = d.filter((m) => m.id !== id)
      setActiveId((a) => (a === id ? next[0]?.id || '' : a))
      return next
    })
    setEditingId((e) => (e === id ? null : e))
  }

  const copyModel = (id: string) => {
    const src = draft.find((m) => m.id === id)
    if (!src) return
    // Duplicate every field but give it a fresh id (so it's a distinct entry)
    // and tag the label so the copy is distinguishable in the list.
    const copy: ModelEntry = {
      ...src,
      id: newId(),
      provider: src.provider ? `${src.provider} Copy` : src.provider,
    }
    setDraft((d) => {
      const idx = d.findIndex((m) => m.id === id)
      if (idx === -1) return [...d, copy]
      const next = [...d]
      next.splice(idx + 1, 0, copy)
      return next
    })
  }

  const handleSave = async () => {
    let active = activeId
    if (!draft.some((m) => m.id === active)) active = draft[0]?.id || ''
    await window.electronAPI.saveModelsConfig({ models: draft, activeId: active })
    await initConfig()
  }

  const sectionTitle = t(SECTIONS.find((s) => s.id === section)?.labelKey || 'settings.title')

  return (
    <div className="flex-1 flex min-h-0">
      {/* Section sidebar */}
      <div className="w-64 flex-shrink-0 border-r border-surface-border flex flex-col">
        <div className="px-3 h-9 flex items-center border-b border-surface-border flex-shrink-0">
          <span className="text-xs font-semibold text-ink-soft">{t('settings.title')}</span>
        </div>
        <div className="flex-1 overflow-y-auto py-1">
          {SECTIONS.map((s) => {
            const isSel = s.id === section
            return (
              <button
                key={s.id}
                onClick={() => setSection(s.id)}
                className={`w-full px-3 py-2 flex items-center gap-2.5 text-left border-l-2 transition-colors ${
                  isSel
                    ? 'bg-sidebar-active border-accent text-accent'
                    : 'border-transparent text-ink-soft hover:bg-sidebar-hover hover:text-ink'
                }`}
              >
                <span className="w-4 text-center flex-shrink-0">{s.icon}</span>
                <span className="text-[13px] font-medium">{t(s.labelKey)}</span>
              </button>
            )
          })}
        </div>
      </div>

      {/* Content pane */}
      <div className="flex-1 flex flex-col min-w-0">
        <div className="px-4 h-9 flex items-center gap-2 border-b border-surface-border flex-shrink-0">
          <span className="text-sm font-semibold text-ink">{sectionTitle}</span>
          <button
            onClick={showChat}
            className="ml-auto text-xs px-2.5 py-1 rounded-md font-semibold border transition-colors bg-accent/10 text-accent border-accent/30 hover:bg-accent/20 hover:border-accent/50"
          >
            {t('settings.backToChat')}
          </button>
        </div>

        <div className="flex-1 overflow-y-auto px-6 py-4">
          {section === 'model' ? (
            <>
              <p className="text-xs text-ink-faint mb-3">{t('settings.model.hint')}</p>
              <div className="space-y-2">
                {draft.length === 0 && (
                  <p className="text-sm text-ink-faint py-4 text-center">{t('settings.model.none')}</p>
                )}
                {draft.map((m) => (
                  <div key={m.id} className="bg-surface-raised rounded-lg border border-surface-border">
                    <div className="flex items-center gap-2 px-3 py-2">
                      <button
                        onClick={() => setActiveId(m.id)}
                        title={activeId === m.id ? t('settings.model.active') : t('settings.model.setActive')}
                        className={`w-4 h-4 rounded-full border shrink-0 ${
                          activeId === m.id ? 'bg-accent border-accent' : 'border-ink-faint hover:border-ink-soft'
                        }`}
                      />
                      <div className="flex-1 min-w-0">
                        <div className="text-sm text-ink truncate">
                          {m.provider} <span className="text-ink-faint">/ {m.model_name || t('settings.model.noModel')}</span>
                        </div>
                        <div className="flex gap-1.5 mt-0.5">
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-border text-ink-soft">
                            OpenAI
                          </span>
                          {m.context_1m && (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-indigo-600/70 text-indigo-100">1M</span>
                          )}
                          {m.max_tokens ? (
                            <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-border text-ink-soft">{PrettyNumber(m.max_tokens)} tok</span>
                          ) : null}
                        </div>
                      </div>
                      <button
                        onClick={() => setEditingId(editingId === m.id ? null : m.id)}
                        className="text-xs text-ink-soft hover:text-ink px-2 py-1"
                      >{editingId === m.id ? t('common.close') : t('common.edit')}</button>
                      <button
                        onClick={() => copyModel(m.id)}
                        className="text-xs text-ink-soft hover:text-ink px-2 py-1"
                        title={t('settings.model.duplicate')}
                      >{t('common.copy')}</button>
                      <button
                        onClick={() => deleteModel(m.id)}
                        className="text-xs text-red-600 hover:text-red-700 px-2 py-1"
                      >{t('common.delete')}</button>
                    </div>

                    {editingId === m.id && (
                      <div className="px-3 pb-3 pt-1 space-y-2 border-t border-surface-border">
                        <Field label={t('settings.model.apiType')}>
                          <select
                            value={m.api_type}
                            onChange={(e) => patch(m.id, { api_type: e.target.value as ModelEntry['api_type'] })}
                            className="w-full bg-surface border border-surface-border text-ink rounded px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-accent"
                          >
                            <option value="openai">{t('settings.model.apiTypeOpenai')}</option>
                          </select>
                        </Field>
                        <Field label={t('settings.model.provider')}>
                          <TextInput value={m.provider} onChange={(v) => patch(m.id, { provider: v })} />
                        </Field>
                        <Field label={t('settings.model.baseUrl')}>
                          <TextInput value={m.base_url} onChange={(v) => patch(m.id, { base_url: v })} placeholder="https://..." />
                        </Field>
                        <Field label={t('settings.model.apiKey')}>
                          <TextInput value={m.api_key} onChange={(v) => patch(m.id, { api_key: v })} password />
                        </Field>
                        <Field label={t('settings.model.modelName')}>
                          <TextInput value={m.model_name} onChange={(v) => patch(m.id, { model_name: v })} />
                        </Field>
                        <Field label={t('settings.model.maxTokens')}>
                          <input
                            type="number"
                            value={m.max_tokens || ''}
                            placeholder="0"
                            min={0}
                            onChange={(e) => {
                              const v = Number(e.target.value) || 0
                              patch(m.id, { max_tokens: v < 0 ? 0 : v })
                            }}
                            className="w-full bg-surface border border-surface-border text-ink rounded px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-accent"
                          />
                        </Field>
                        <label className="flex items-center gap-2 text-sm text-ink-soft pt-1">
                          <input
                            type="checkbox"
                            checked={m.context_1m}
                            onChange={(e) => patch(m.id, { context_1m: e.target.checked })}
                            className="accent-accent"
                          />
                          {t('settings.model.context1m')}
                        </label>
                        <Field label={t('settings.model.reasoningValues')}>
                          <input
                            type="text"
                            value={(m.reasoning_efforts || []).join(', ')}
                            onChange={(e) => patch(m.id, { reasoning_efforts: e.target.value.split(',').map((v) => v.trim()).filter(Boolean) })}
                            placeholder="e.g. low, high, max"
                            className="w-full bg-surface border border-surface-border text-ink rounded px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-accent"
                          />
                        </Field>
                        <ReasoningHint m={m} />
                      </div>
                    )}
                  </div>
                ))}
              </div>

              <button
                onClick={addModel}
                className="mt-3 w-full px-3 py-2 border border-dashed border-surface-border text-ink-soft hover:text-ink hover:border-ink-faint text-sm rounded-lg"
              >{t('settings.model.add')}</button>

              <div className="mt-6 flex justify-end gap-2">
                <button onClick={showChat} className="px-4 py-2 bg-surface-raised hover:bg-sidebar-hover text-ink text-sm rounded-lg border border-surface-border">{t('common.discard')}</button>
                <button onClick={handleSave} className="px-4 py-2 bg-accent hover:bg-accent-hover text-accent-ink text-sm rounded-lg">{t('common.save')}</button>
              </div>
            </>
          ) : section === 'theme' ? (
            <>
              <div className="grid grid-cols-2 gap-3">
                {THEMES.map((t2) => (
                  <button
                    key={t2.id}
                    onClick={() => setTheme(t2.id)}
                    className={`flex items-center gap-3 rounded-lg border p-2.5 text-left transition-colors ${
                      theme === t2.id
                        ? 'border-accent ring-1 ring-accent'
                        : 'border-surface-border hover:bg-surface-raised'
                    }`}
                  >
                    <span
                      className="flex rounded-md overflow-hidden border border-surface-border flex-shrink-0"
                      style={{ width: 44, height: 28 }}
                    >
                      <span style={{ flex: 1, background: t2.swatch[0] }} />
                      <span style={{ flex: 1, background: t2.swatch[1] }} />
                      <span style={{ flex: 1, background: t2.swatch[2] }} />
                    </span>
                    <span className="flex-1 min-w-0">
                      <span className="block text-sm text-ink font-medium truncate">{t2.name}</span>
                      <span className="block text-[11px] text-ink-faint">{t2.tone === 'dark' ? t('settings.theme.dark') : t('settings.theme.light')}</span>
                    </span>
                    {theme === t2.id && <span className="text-accent text-sm">✓</span>}
                  </button>
                ))}
              </div>
              <p className="mt-3 text-xs text-ink-faint">{t('settings.theme.appliesInstantly')}</p>
            </>
          ) : section === 'font' ? (
            <>
              <p className="text-xs text-ink-faint mb-3">{t('settings.font.hint')}</p>

              <div className="mb-5">
                <div className="text-xs font-semibold text-ink mb-2">{t('settings.font.family')}</div>
                <div className="grid grid-cols-2 gap-2">
                  {FONT_OPTIONS.map((f) => (
                    <button
                      key={f.id}
                      onClick={() => setFontFamily(f.id)}
                      className={`rounded-lg border p-2.5 text-left transition-colors ${
                        fontFamily === f.id
                          ? 'border-accent ring-1 ring-accent'
                          : 'border-surface-border hover:bg-surface-raised'
                      }`}
                    >
                      <span className="block text-sm text-ink" style={{ fontFamily: f.stack }}>{f.name}</span>
                      <span className="block text-[11px] text-ink-faint">{f.note}</span>
                    </button>
                  ))}
                </div>
              </div>

              <div className="mb-3">
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-semibold text-ink">{t('settings.font.size')} <span className="text-ink-faint font-normal">{t('settings.font.sizeUiScale')}</span></span>
                  <span className="text-xs text-ink tabular-nums">{fontSize}px</span>
                </div>
                <input
                  type="range"
                  min={10}
                  max={30}
                  step={1}
                  value={fontSize}
                  onChange={(e) => setFontSize(Number(e.target.value))}
                  className="w-full accent-accent cursor-pointer"
                />
                <div className="flex justify-between text-[10px] text-ink-faint mt-1">
                  <span>10</span>
                  <span>30</span>
                </div>
                <p className="text-[11px] text-ink-faint mt-2">{t('settings.font.scalesUi')}</p>
              </div>
            </>
          ) : section === 'sandbox' ? (
            <>
              <p className="text-xs text-ink-faint mb-3">{t('settings.sandbox.hint')}</p>

              {!grantsLoaded ? (
                <p className="text-sm text-ink-faint py-4 text-center">{t('common.loading')}</p>
              ) : grants.length === 0 ? (
                <p className="text-sm text-ink-faint py-4 text-center">{t('settings.sandbox.none')}</p>
              ) : (
                <div className="space-y-2">
                  {grants.map((g) => (
                    <div key={g} className="flex items-center gap-2 bg-surface-raised rounded-lg border border-surface-border px-3 py-2">
                      <span className="flex-1 min-w-0 text-sm text-ink font-mono truncate" title={g}>{g}</span>
                      <button
                        onClick={() => removeGrant(g)}
                        className="text-xs text-red-600 hover:text-red-700 px-2 py-1 shrink-0"
                      >{t('settings.sandbox.remove')}</button>
                    </div>
                  ))}
                </div>
              )}

              <button
                onClick={addGrant}
                className="mt-3 w-full px-3 py-2 border border-dashed border-surface-border text-ink-soft hover:text-ink hover:border-ink-faint text-sm rounded-lg"
              >{t('settings.sandbox.addFolder')}</button>

              <p className="mt-3 text-[11px] text-ink-faint">{t('settings.sandbox.footnote')}</p>

              <div className="mt-6 pt-4 border-t border-surface-border">
                <p className="text-sm font-semibold text-ink mb-1">{t('settings.sandbox.forbidRead.title')}</p>
                <p className="text-xs text-ink-faint mb-3">{t('settings.sandbox.forbidRead.hint')}</p>

                {!forbidReadLoaded ? (
                  <p className="text-sm text-ink-faint py-4 text-center">{t('common.loading')}</p>
                ) : forbidRead.length === 0 ? (
                  <p className="text-sm text-ink-faint py-4 text-center">{t('settings.sandbox.forbidRead.none')}</p>
                ) : (
                  <div className="space-y-2">
                    {forbidRead.map((f) => (
                      <div key={f} className="flex items-center gap-2 bg-surface-raised rounded-lg border border-surface-border px-3 py-2">
                        <span className="flex-1 min-w-0 text-sm text-ink font-mono truncate" title={f}>{f}</span>
                        <button
                          onClick={() => removeForbidRead(f)}
                          className="text-xs text-red-600 hover:text-red-700 px-2 py-1 shrink-0"
                        >{t('settings.sandbox.remove')}</button>
                      </div>
                    ))}
                  </div>
                )}

                <button
                  onClick={addForbidRead}
                  className="mt-3 w-full px-3 py-2 border border-dashed border-surface-border text-ink-soft hover:text-ink hover:border-ink-faint text-sm rounded-lg"
                >{t('settings.sandbox.forbidRead.addFolder')}</button>

                <p className="mt-3 text-[11px] text-ink-faint">{t('settings.sandbox.forbidRead.footnote')}</p>
              </div>

              <div className="mt-6 pt-4 border-t border-surface-border">
                <p className="text-sm font-semibold text-ink mb-1">{t('settings.sandbox.ssrf.title')}</p>
                <p className="text-xs text-ink-faint mb-3">{t('settings.sandbox.ssrf.hint')}</p>

                {/* Allowed hosts */}
                <p className="text-xs font-semibold text-ink mb-1">{t('settings.sandbox.ssrf.allow.title')}</p>
                {!ssrfLoaded ? (
                  <p className="text-sm text-ink-faint py-2">{t('common.loading')}</p>
                ) : ssrfAllow.length === 0 ? (
                  <p className="text-sm text-ink-faint py-2">{t('settings.sandbox.ssrf.allow.none')}</p>
                ) : (
                  <div className="space-y-1 mb-1">
                    {ssrfAllow.map((a) => (
                      <div key={a} className="flex items-center gap-2 bg-surface-raised rounded-lg border border-surface-border px-3 py-1.5">
                        <span className="flex-1 min-w-0 text-sm text-ink font-mono truncate" title={a}>{a}</span>
                        {a.includes('169.254') && (
                          <span className="text-[10px] text-amber-600 shrink-0">{t('settings.sandbox.ssrf.metadataWarning')}</span>
                        )}
                        <button onClick={() => commitSsr(ssrfAllow.filter((x) => x !== a), ssrfBlockExtra)}
                          className="text-xs text-red-600 hover:text-red-700 px-2 py-1 shrink-0">{t('settings.sandbox.ssrf.remove')}</button>
                      </div>
                    ))}
                  </div>
                )}
                <div className="flex gap-2 mt-2">
                  <input
                    value={ssrfAllowInput}
                    onChange={(e) => setSsrAllowInput(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter') addSsrAllow() }}
                    placeholder={t('settings.sandbox.ssrf.placeholder')}
                    className="flex-1 min-w-0 px-3 py-1.5 text-sm bg-surface-raised border border-surface-border rounded-lg text-ink outline-none focus:border-ink-faint"
                  />
                  <button onClick={addSsrAllow}
                    className="px-3 py-1.5 text-sm border border-dashed border-surface-border rounded-lg text-ink-soft hover:text-ink shrink-0">{t('settings.sandbox.ssrf.add')}</button>
                </div>

                {/* block_extra — same pattern, state ssrfBlockExtra / ssrfBlockInput */}
                <div className="mt-4">
                  <p className="text-xs font-semibold text-ink mb-1">{t('settings.sandbox.ssrf.blockExtra.title')}</p>
                  {!ssrfLoaded ? (
                    <p className="text-sm text-ink-faint py-2">{t('common.loading')}</p>
                  ) : ssrfBlockExtra.length === 0 ? (
                    <p className="text-sm text-ink-faint py-2">{t('settings.sandbox.ssrf.blockExtra.none')}</p>
                  ) : (
                    <div className="space-y-1 mb-1">
                      {ssrfBlockExtra.map((b) => (
                        <div key={b} className="flex items-center gap-2 bg-surface-raised rounded-lg border border-surface-border px-3 py-1.5">
                          <span className="flex-1 min-w-0 text-sm text-ink font-mono truncate" title={b}>{b}</span>
                          <button onClick={() => commitSsr(ssrfAllow, ssrfBlockExtra.filter((x) => x !== b))}
                            className="text-xs text-red-600 hover:text-red-700 px-2 py-1 shrink-0">{t('settings.sandbox.ssrf.remove')}</button>
                        </div>
                      ))}
                    </div>
                  )}
                  <div className="flex gap-2 mt-2">
                    <input
                      value={ssrfBlockInput}
                      onChange={(e) => setSsrBlockInput(e.target.value)}
                      onKeyDown={(e) => { if (e.key === 'Enter') addSsrBlockExtra() }}
                      placeholder={t('settings.sandbox.ssrf.placeholder')}
                      className="flex-1 min-w-0 px-3 py-1.5 text-sm bg-surface-raised border border-surface-border rounded-lg text-ink outline-none focus:border-ink-faint"
                    />
                    <button onClick={addSsrBlockExtra}
                      className="px-3 py-1.5 text-sm border border-dashed border-surface-border rounded-lg text-ink-soft hover:text-ink shrink-0">{t('settings.sandbox.ssrf.add')}</button>
                  </div>
                </div>

                {/* Built-in denied ranges (read-only) */}
                <div className="mt-4">
                  <p className="text-xs font-semibold text-ink mb-1">{t('settings.sandbox.ssrf.defaults.title')}</p>
                  <div className="flex flex-wrap gap-1">
                    {DEFAULT_BLOCKED_NETS.map((n) => (
                      <span key={n} className="text-[10px] px-1.5 py-0.5 rounded bg-surface-raised border border-surface-border text-ink-faint font-mono">{n}</span>
                    ))}
                  </div>
                </div>

                <p className="mt-3 text-[11px] text-ink-faint">{t('settings.sandbox.ssrf.footnote')}</p>
              </div>
            </>
          ) : (
            <>
              <p className="text-xs text-ink-faint mb-3">{t('settings.language.hint')}</p>

              <div className="grid grid-cols-2 gap-3">
                {([
                  { id: 'en' as const, labelKey: 'settings.language.english' as MessageKey, native: 'English' },
                  { id: 'zh' as const, labelKey: 'settings.language.chinese' as MessageKey, native: '中文' },
                ]).map((l) => (
                  <button
                    key={l.id}
                    onClick={() => setLanguage(l.id)}
                    className={`flex items-center gap-3 rounded-lg border p-2.5 text-left transition-colors ${
                      lang === l.id
                        ? 'border-accent ring-1 ring-accent'
                        : 'border-surface-border hover:bg-surface-raised'
                    }`}
                  >
                    <span className="flex-1 min-w-0">
                      <span className="block text-sm text-ink font-medium">{l.native}</span>
                      <span className="block text-[11px] text-ink-faint">{t(l.labelKey)}</span>
                    </span>
                    {lang === l.id && <span className="text-accent text-sm">✓</span>}
                  </button>
                ))}
              </div>
              <p className="mt-3 text-xs text-ink-faint">{t('settings.theme.appliesInstantly')}</p>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="block text-xs text-ink-soft mb-1">{label}</label>
      {children}
    </div>
  )
}

function TextInput({ value, onChange, password, placeholder }: {
  value: string; onChange: (v: string) => void; password?: boolean; placeholder?: string
}) {
  return (
    <input
      type={password ? 'password' : 'text'}
      value={value}
      placeholder={placeholder}
      onChange={(e) => onChange(e.target.value)}
      className="w-full bg-surface border border-surface-border text-ink rounded px-2 py-1.5 text-sm outline-none focus:ring-1 focus:ring-accent"
    />
  )
}

function PrettyNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`
  return String(n)
}

// Live hint under the reasoning-value editor: shows the preset that the current
// model name resolves to (ignoring any override), plus the "when unsure, pick
// default" reminder.
function ReasoningHint({ m }: { m: ModelEntry }) {
  const t = useT()
  const overridden = !!(m.reasoning_efforts && m.reasoning_efforts.length > 0)
  const base = { ...m, reasoning_efforts: undefined }
  const preset = reasoningValuesFor(base)
  return (
    <div className="text-[10px] text-ink-faint space-y-0.5">
      {overridden ? (
        <p>{t('settings.model.reasoningHint.custom')}</p>
      ) : (
        <p>
          {t('settings.model.reasoningHint.preset', {
            model: m.model_name || t('settings.model.noModel'),
            values: preset.length ? preset.join(', ') : t('settings.model.reasoningHint.none'),
          })}
        </p>
      )}
      <p>{t('settings.model.reasoningHint.advice')}</p>
    </div>
  )
}
