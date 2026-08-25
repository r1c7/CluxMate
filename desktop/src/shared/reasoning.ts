// Reasoning-effort dialects, system presets, and wire translation (the browser
// half mirrors cluxmate/core/reasoning.py). Values are RAW provider enum
// strings — no label translation. A model entry's `reasoning_efforts` list
// overrides the preset. There is no per-model default: every model starts on
// the "default" sentinel (send no reasoning fields).
import type { ModelEntry } from './types'

type Dialect = 'openai' | 'deepseek' | 'glm' | 'qwen'

const PRESETS: Record<Dialect, { values: string[]; default: string }> = {
  openai: { values: ['minimal', 'low', 'medium', 'high'], default: 'medium' },
  deepseek: { values: ['low', 'high', 'max'], default: 'high' },
  glm: { values: ['max', 'xhigh', 'high', 'medium', 'low', 'minimal', 'none'], default: 'max' },
  qwen: { values: ['low', 'medium', 'xhigh'], default: 'xhigh' },
}

export function detectDialect(modelName: string, baseUrl: string | undefined, provider: string): Dialect {
  const mn = modelName.toLowerCase()
  if (mn.includes('deepseek')) return 'deepseek'
  if (mn.includes('glm') || mn.includes('zhipu') || mn.includes('bigmodel')) return 'glm'
  if (mn.includes('qwen') || mn.includes('dashscope') || mn.includes('alibaba')) return 'qwen'
  const key = `${baseUrl || ''} ${provider}`.toLowerCase()
  if (key.includes('deepseek')) return 'deepseek'
  if (key.includes('bigmodel') || key.includes('zhipu') || key.includes('glm')) return 'glm'
  if (key.includes('dashscope') || key.includes('qwen') || key.includes('alibaba')) return 'qwen'
  return 'openai'
}

export function reasoningValuesFor(entry: ModelEntry | undefined): string[] {
  if (!entry) return []
  if (entry.reasoning_efforts && entry.reasoning_efforts.length > 0) {
    return entry.reasoning_efforts.filter((v) => v.trim() !== '')
  }
  return [...PRESETS[detectDialect(entry.model_name, entry.base_url, entry.provider)].values]
}

export function defaultReasoningValue(entry: ModelEntry | undefined): string | null {
  const values = reasoningValuesFor(entry)
  if (values.length === 0) return null
  // The universal default is "default" (send no reasoning fields) — we do NOT
  // preselect the per-dialect preset default.
  return DEFAULT_EFFORT
}

// The "provider default" sentinel: selecting it sends no reasoning fields at
// all, deferring to the server. It is a runtime choice, not a wire value.
export const DEFAULT_EFFORT = 'default'

export function reasoningOptionsFor(entry: ModelEntry | undefined): string[] {
  return [DEFAULT_EFFORT, ...reasoningValuesFor(entry)]
}
