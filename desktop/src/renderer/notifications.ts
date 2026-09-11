// Preference for the taskbar attention signal (see main/attention.ts): flash the
// window while a permission prompt / question card is waiting and the window is
// in the background. Mirrors themes.ts / fonts.ts — a thin module owning the
// storage key + best-effort persistence, with the value mirrored in the store.

export const ATTENTION_FLASH_STORAGE_KEY = 'cluxmate.attentionFlash'

// Default on: an agent blocked on a human is easy to miss otherwise, and the
// signal is passive (no sound, no popup).
export function loadAttentionFlash(): boolean {
  try {
    return localStorage.getItem(ATTENTION_FLASH_STORAGE_KEY) !== '0'
  } catch {
    return true
  }
}

export function saveAttentionFlash(enabled: boolean): boolean {
  try {
    localStorage.setItem(ATTENTION_FLASH_STORAGE_KEY, enabled ? '1' : '0')
  } catch {
    // Best effort (storage disabled) — the choice still applies this session.
  }
  return enabled
}
