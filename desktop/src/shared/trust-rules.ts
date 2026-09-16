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

/**
 * Does a written trust decision have to cost the live bridge a restart?
 *
 * The running Python process holds the hooks, MCP clients, skills, subagents and
 * always-allow rules it built from the answer it was spawned with, and it only
 * rebuilds them on the next `initialize`. So ANY change of the answer — a grant,
 * a denial, or a removal back to undecided — has to kill it, or a revocation
 * leaves the agent running with exactly the config the user just took away
 * (the Python side's own `trust/*` handlers only rewrite the registry). An
 * unchanged answer, or a change to a directory this process is not running in,
 * must not: the session would pay a re-initialize for nothing.
 *
 * `statusBefore`/`statusAfter` are the resolved answers for `cwd` read off the
 * live process around the write, `liveCwd` the directory that process was
 * spawned for (null when there is none), and `sameDir` the caller's path
 * comparison — this module stays filesystem-free.
 */
export function trustChangeRestartsBridge(
  statusBefore: string | null | undefined,
  statusAfter: string | null | undefined,
  cwd: string,
  liveCwd: string | null | undefined,
  sameDir: (a: string, b: string) => boolean,
): boolean {
  if (!liveCwd || !statusBefore || !statusAfter) return false
  if (!sameDir(cwd, liveCwd)) return false
  return statusBefore !== statusAfter
}
