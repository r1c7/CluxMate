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

/**
 * What a project-trust call owes the SESSION's bridge.
 *
 * `warmCwd` is the directory that bridge has to be running in, and it is always
 * the session's own cwd. The trust TARGET travels as a plain JSON-RPC parameter
 * (the Python side resolves `params["cwd"]`, falling back to the session's cwd),
 * so the target is allowed to be a directory this session does not run in — the
 * settings list revokes any recorded directory. Warming at the target instead
 * makes `ensureBridge` kill the live process and respawn it over there (it
 * respawns whenever the cwd it is handed differs from the live one): an
 * in-flight turn dies with the process, and the replacement runs the OTHER
 * project's `SessionStart` hooks while that project's entry is still trusted —
 * and dies again when the revoke this call was warming for lands.
 *
 * `targetIsSessionDir` is the half that concerns the session-only trust map: a
 * decision is only ever re-sent to a spawn of THIS session, at its own
 * directory, so an answer about a foreign directory has no consumer.
 */
export interface TrustCallPlan {
  warmCwd: string
  targetIsSessionDir: boolean
}

export function planTrustCall(
  targetCwd: string,
  sessionCwd: string | null | undefined,
  sameDir: (a: string, b: string) => boolean,
): TrustCallPlan {
  const session = sessionCwd ?? ''
  return {
    warmCwd: session,
    // A session with no directory can be neither spawned at nor compared, so a
    // call about it may not file a decision anywhere either.
    targetIsSessionDir: session !== '' && sameDir(targetCwd, session),
  }
}
