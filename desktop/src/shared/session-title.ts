// Derive a session title from the user's first message: first non-empty line,
// trimmed and capped so the DB doesn't store an entire prompt. The sidebar
// also CSS-truncates by width, so this only bounds the stored string.
const MAX_TITLE_LEN = 50

export function deriveSessionTitle(input: string): string {
  const firstLine = (input || '').split('\n').map((l) => l.trim()).find((l) => l.length > 0) || ''
  if (!firstLine) return 'New Session'
  return firstLine.length > MAX_TITLE_LEN
    ? firstLine.slice(0, MAX_TITLE_LEN).trimEnd() + '...'
    : firstLine
}
