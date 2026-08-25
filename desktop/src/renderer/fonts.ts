// Font settings for the desktop shell. The family choice targets the code /
// monospace font (what developers actually read code in); the size controls the
// global root font-size that rescales the whole UI. Both are persisted and
// applied to <html> (inline style overrides the CSS default) so they survive
// restarts and apply before first paint.

export interface FontOption {
  id: string
  name: string
  /** Short availability hint (e.g. "Windows default"). */
  note: string
  /** Full `font-family` value, primary face first with fallbacks. */
  stack: string
}

export const DEFAULT_FONT_ID = 'system'
export const DEFAULT_FONT_SIZE = 15
export const FONT_STORAGE_KEY = 'cluxmate.font-family'
export const SIZE_STORAGE_KEY = 'cluxmate.font-size'

// Only OS-bundled / near-universal faces — every entry has a fallback chain
// that ends in a guaranteed face, so nothing ever renders as a broken font.
export const FONT_OPTIONS: FontOption[] = [
  { id: 'system', name: 'System Mono', note: 'Auto — matches OS', stack: "ui-monospace, 'Cascadia Code', Menlo, Consolas, monospace" },
  { id: 'consolas', name: 'Consolas', note: 'Windows default', stack: "Consolas, 'Courier New', monospace" },
  { id: 'cascadia', name: 'Cascadia Code', note: 'Windows 10/11', stack: "'Cascadia Code', Consolas, monospace" },
  { id: 'courier', name: 'Courier New', note: 'Universal classic', stack: "'Courier New', Courier, monospace" },
]

function fontStack(id: string | null | undefined): string {
  return FONT_OPTIONS.find((o) => o.id === id)?.stack ?? FONT_OPTIONS[0].stack
}

export function applyFontFamily(id: string): string {
  const fid = FONT_OPTIONS.some((o) => o.id === id) ? id : DEFAULT_FONT_ID
  document.documentElement.style.setProperty('--font-mono', fontStack(fid))
  return fid
}

export function applyFontSize(size: number): number {
  const s = Math.min(30, Math.max(10, Math.round(size) || DEFAULT_FONT_SIZE))
  document.documentElement.style.fontSize = `${s}px`
  return s
}

export function saveFontFamily(id: string): string {
  const fid = applyFontFamily(id)
  try { localStorage.setItem(FONT_STORAGE_KEY, fid) } catch { /* best effort */ }
  return fid
}

export function saveFontSize(size: number): number {
  const s = applyFontSize(size)
  try { localStorage.setItem(SIZE_STORAGE_KEY, String(s)) } catch { /* best effort */ }
  return s
}

export function loadFontFamily(): string {
  try {
    const id = localStorage.getItem(FONT_STORAGE_KEY)
    return FONT_OPTIONS.some((o) => o.id === id) ? id! : DEFAULT_FONT_ID
  } catch { return DEFAULT_FONT_ID }
}

export function loadFontSize(): number {
  try {
    const s = parseInt(localStorage.getItem(SIZE_STORAGE_KEY) || '', 10)
    return s >= 10 && s <= 30 ? s : DEFAULT_FONT_SIZE
  } catch { return DEFAULT_FONT_SIZE }
}
