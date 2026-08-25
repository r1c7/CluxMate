// Theme registry for the desktop shell. Each theme's color tokens live as CSS
// custom properties (RGB triplets) in App.css under `:root` / `[data-theme=…]`;
// tailwind.config.js maps its color names onto those variables via
// `rgb(var(--…) / <alpha-value>)`. This module only owns the *list* of themes,
// the persisted selection, and applying the `data-theme` attribute to <html>.

export interface ThemeDef {
  id: string
  name: string
  tone: 'light' | 'dark'
  /** [chat, sidebar, accent] — used for the Settings picker swatch. */
  swatch: [string, string, string]
}

export const DEFAULT_THEME = 'cream'
export const THEME_STORAGE_KEY = 'cluxmate.theme'

export const THEMES: ThemeDef[] = [
  { id: 'cream', name: 'Cream', tone: 'light', swatch: ['#fbfaf7', '#f4f1ea', '#c2684a'] },
  { id: 'stone', name: 'Stone', tone: 'light', swatch: ['#ffffff', '#f6f5f2', '#b45309'] },
  { id: 'porcelain', name: 'Porcelain', tone: 'light', swatch: ['#ffffff', '#f6f4f4', '#c0392b'] },
  { id: 'solarized', name: 'Solarized', tone: 'light', swatch: ['#f9f7f0', '#f0ede3', '#b95015'] },
  { id: 'onelight', name: 'One Light', tone: 'light', swatch: ['#fafafa', '#fafafa', '#526FFF'] },
  { id: 'sage', name: 'Sage', tone: 'light', swatch: ['#f5f6f1', '#e9ece3', '#4f7a5b'] },
  { id: 'github', name: 'GitHub', tone: 'light', swatch: ['#ffffff', '#f6f8fa', '#0969da'] },
  { id: 'nord', name: 'Nord', tone: 'dark', swatch: ['#2e3440', '#2a2f3a', '#88c0d0'] },
  { id: 'onedark', name: 'One Dark', tone: 'dark', swatch: ['#282c34', '#21252b', '#61afef'] },
  { id: 'solarizeddark', name: 'Solarized Dark', tone: 'dark', swatch: ['#002b36', '#073642', '#268bd2'] },
  { id: 'vscodedark', name: 'VS Code Dark', tone: 'dark', swatch: ['#181818', '#141414', '#339cff'] },
  { id: 'githubdark', name: 'GitHub Dark', tone: 'dark', swatch: ['#111111', '#0d0d0d', '#0169cc'] },
]

export function isTheme(id: string | null | undefined): id is string {
  return !!id && THEMES.some((t) => t.id === id)
}

export function isDarkTheme(id: string): boolean {
  return THEMES.find((t) => t.id === id)?.tone === 'dark'
}

/** Apply `id` to <html> (falling back to the default) and return the effective id. */
export function applyTheme(id: string | null | undefined): string {
  const themeId = isTheme(id) ? id : DEFAULT_THEME
  document.documentElement.setAttribute('data-theme', themeId)
  return themeId
}

/** Persist + apply; returns the effective id. */
export function saveTheme(id: string): string {
  const themeId = applyTheme(id)
  try {
    localStorage.setItem(THEME_STORAGE_KEY, themeId)
  } catch {
    // Persistence is best-effort (e.g. storage disabled) — the session still themes.
  }
  return themeId
}
