// Sidebar layout persistence for the desktop shell. The left session sidebar is
// resizable by dragging its right edge; the width is persisted (best-effort
// localStorage, mirroring theme/font) so it survives restarts.

export const SIDEBAR_WIDTH_STORAGE_KEY = 'cluxmate.sidebar-width'
export const DEFAULT_SIDEBAR_WIDTH = 260
export const SIDEBAR_MIN_WIDTH = 200
export const SIDEBAR_MAX_WIDTH = 480

/** Clamp an arbitrary width to the allowed range; non-finite → default. */
export function clampSidebarWidth(w: number): number {
  const n = Math.round(w)
  if (!Number.isFinite(n)) return DEFAULT_SIDEBAR_WIDTH
  return Math.min(SIDEBAR_MAX_WIDTH, Math.max(SIDEBAR_MIN_WIDTH, n))
}

export function loadSidebarWidth(): number {
  try {
    return clampSidebarWidth(parseInt(localStorage.getItem(SIDEBAR_WIDTH_STORAGE_KEY) || '', 10))
  } catch {
    return DEFAULT_SIDEBAR_WIDTH
  }
}

export function saveSidebarWidth(w: number): number {
  const clamped = clampSidebarWidth(w)
  try {
    localStorage.setItem(SIDEBAR_WIDTH_STORAGE_KEY, String(clamped))
  } catch {
    // Persistence is best-effort (e.g. storage disabled) — the session still resizes.
  }
  return clamped
}
