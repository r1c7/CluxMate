// Pure project-trust rules shared by the main process and the renderer store
// (same pattern as skill-rules.ts): the prompt decision must not be re-derived
// differently in the two halves of the app.

export type TrustStatus = 'trusted' | 'denied' | 'unknown'

export interface TrustFinding {
  kind: string
  label: string
  path: string
}

export interface TrustSnapshot {
  cwd: string
  status: TrustStatus
  source: string
  findings: TrustFinding[]
  store: Record<string, string>
}

/** The directory has project config that is currently NOT being loaded. */
export function isUntrusted(snapshot: TrustSnapshot | null | undefined): boolean {
  if (!snapshot) return false
  return snapshot.status !== 'trusted' && snapshot.findings.length > 0
}

/** Ask the user: undecided AND there is something to decide about. */
export function shouldPromptTrust(snapshot: TrustSnapshot | null | undefined): boolean {
  if (!snapshot) return false
  return snapshot.status === 'unknown' && snapshot.findings.length > 0
}

/** One-line list of the withheld config families, for banners and cards. */
export function trustSummary(snapshot: TrustSnapshot | null | undefined): string {
  if (!snapshot) return ''
  return snapshot.findings.map((f) => f.label).join(', ')
}
