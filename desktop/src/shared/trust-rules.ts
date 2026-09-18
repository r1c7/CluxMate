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

/**
 * May this snapshot (re)open the trust card?
 *
 * `fetchEpoch` is the value of the renderer's answer counter when the fetch
 * started, `currentEpoch` the value now: if the user answered while the reply
 * was in flight, the reply describes the decision they just replaced, and
 * re-opening the card would fight them. Otherwise the snapshot is the
 * authoritative view, so an undecided directory with withheld config gets its
 * card back even when the pushing notification was lost.
 */
export function shouldPromptFromFetch(
  snapshot: TrustSnapshot | null | undefined,
  fetchEpoch: number,
  currentEpoch: number,
): boolean {
  if (fetchEpoch !== currentEpoch) return false
  return shouldPromptTrust(snapshot)
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

/** A live bridge, described by the directory it was spawned for. */
export interface LiveBridge {
  sessionId: string
  cwd: string | null | undefined
}

/**
 * Every live bridge whose answer changed — the whole set, not the one that
 * served the call.
 *
 * Settings can revoke a row for ANY recorded directory, and the call is
 * answered by whichever session is active: a revoke of directory X issued from
 * session B must still tear down session A's process, which is the one holding
 * X's hooks / MCP clients / skills / always-allow rules. So the per-bridge rule
 * above is applied to every live bridge, and the ones running in the target
 * directory are returned.
 */
export function bridgesToRestart(
  statusBefore: string | null | undefined,
  statusAfter: string | null | undefined,
  targetCwd: string,
  liveBridges: readonly LiveBridge[],
  sameDir: (a: string, b: string) => boolean,
): string[] {
  return liveBridges
    .filter((b) => trustChangeRestartsBridge(statusBefore, statusAfter, targetCwd, b.cwd, sameDir))
    .map((b) => b.sessionId)
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

// ── §E: the project CONFIG root vs the execution tree ───────────────────────
// The rule the whole desktop config layer hangs on, in the same pure module as
// the trust helpers because the trust target below is derived from it: a
// session's project config root is `project_root || cwd`. Kept here (not in
// ipc-handlers.ts) so desktop/tests can exercise it without an Electron
// harness — getting it backwards silently points the panels at another tree.

// A session's §E project config root — permissions.json, mcp.json, skills.json,
// settings.json, memory facts, AGENTS.md and the trust key all resolve through
// it — read structurally, so this module still has no dependency on
// shared/types.ts.
//
// `fallback` is the caller's `cwd`: a row written before the project_root column
// existed has it NULL, and `project_root || cwd` must then be the row's own cwd
// (today's behaviour), never an empty string. A session with no row at all has
// no config root to report, so the caller's fallback stands alone.
export function projectConfigRoot(
  session: { project_root?: string | null } | null | undefined,
  fallback: string,
): string {
  return session?.project_root || fallback
}

// The directory a TRUST call is about, as the trust subsystem files it.
//
// Python resolves every trust target to that directory's PROJECT CONFIG ROOT —
// both the registry rows and the answer a process loaded are keyed there — so a
// call about a session's own execution tree and a call about its project root
// are the same call about the same config. Normalizing here is what keeps the
// two spellings from becoming two different answers: the renderer asks about the
// tree the session runs in (`workingDir`), while the session-only decision map
// and the bridge-restart guard are keyed on the project root. Without it, a
// worktree session's card would neither file its "just this once" answer where
// the next spawn reads it, nor restart the processes that loaded the answer that
// changed.
//
// A target that is NOT this session's own tree travels unchanged: the Settings
// list revokes any recorded row, and rows are project roots already.
//
// `sameDir` is the caller's own path comparison, the same injection the rest of
// this module takes (`sameCwd` in the main process): the comparison resolves
// symlinks and canonical case, which is filesystem work this module deliberately
// does not do.
export function trustTargetDir(
  cwd: string,
  meta: { cwd: string },
  projectRoot: string,
  sameDir: (a: string, b: string) => boolean,
): string {
  return sameDir(cwd, meta.cwd) ? projectRoot : cwd
}
