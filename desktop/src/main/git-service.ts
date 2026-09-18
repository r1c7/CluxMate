import { execFile } from 'child_process'
import * as fs from 'fs'
import * as path from 'path'
import type { GitInfo, GitBranchList, GitBranchOptions, GitCheckoutStrategy, GitCheckoutResult } from '../shared/types'
import { worktreeInfo, worktreeList } from './worktree.ts'
import type { WorktreeInfoResult, WorktreeResult } from './worktree.ts'
import {
  NO_CONTAINER,
  commitGuardRefusal,
  withoutContainerLines,
  type WorktreeContainer,
} from '../shared/worktree-container.ts'
// The pure half of "which branches may the PROJECT switch to" — the branch pill
// hides itself inside a worktree and the main tree's list drops the branches a
// linked worktree holds. Rules only; the git facts come from the CLI above.
import { withoutWorktreeBranches } from '../shared/worktree-rules.ts'

// Run git directly (no shell) against a working directory. Mirrors the Python
// side's CheckpointManager._run pattern (resolve binary, direct argv) but lives
// in the main process so it works even when the per-session Python agent bridge
// is cold. All commands resolve the repo root first via `rev-parse --show-toplevel`
// so a nested session cwd behaves correctly — `git clean`/`reset` operate on the
// whole work tree, not a subdirectory.

function runGit(cwd: string, args: string[]): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile('git', args, { cwd, encoding: 'utf8', windowsHide: true }, (err, stdout, stderr) => {
      if (err) {
        // git writes its diagnostic to stderr; prefer it over the generic
        // execFile message so the renderer can surface a meaningful error.
        const message = (stderr || '').trim() || err.message
        reject(new Error(message))
        return
      }
      resolve(stdout)
    })
  })
}

// Resolve the repo root for a cwd, or null when the directory is not inside a
// git repository (or git is missing). Also nulls on a bare/unborn HEAD edge.
async function repoRoot(cwd: string): Promise<string | null> {
  try {
    return (await runGit(cwd, ['rev-parse', '--show-toplevel'])).trim() || null
  } catch {
    return null
  }
}

async function currentBranch(root: string): Promise<string | null> {
  try {
    return (await runGit(root, ['symbolic-ref', '--short', 'HEAD'])).trim() || null
  } catch {
    // Detached HEAD (or empty repo) — fall back to the short commit sha.
    try {
      return (await runGit(root, ['rev-parse', '--short', 'HEAD'])).trim() || null
    } catch {
      return null
    }
  }
}

// --- the worktree container --------------------------------------------------
// CluxMate's own `<repo>/.worktrees` must never reach a reader as one of the
// user's uncommitted changes. WHICH directory that is comes from ONE place —
// `cluxmate worktree info` (main/worktree.ts) — and the filtering rule itself
// lives in shared/worktree-container.ts, so a caller can unit-test it without
// python or a repository. The lookup is fail-safe: a missing interpreter,
// "not a repository" or an older CLI all degrade to NO_CONTAINER, which filters
// nothing and keeps today's behaviour.
async function containerFor(cwd: string): Promise<WorktreeContainer> {
  return containerFrom(await worktreeInfo(cwd))
}

// The container as every reader below needs it. `gitInfo` also reads
// `is_worktree` off the SAME payload, so one `cluxmate worktree info` call keeps
// answering both questions — one subprocess, one project-root policy.
function containerFrom(info: WorktreeResult<WorktreeInfoResult>): WorktreeContainer {
  return info.ok ? { name: info.container, ignored: info.container_ignored } : NO_CONTAINER
}

async function hasChanges(root: string, container: WorktreeContainer = NO_CONTAINER): Promise<boolean> {
  try {
    const out = await runGit(root, ['status', '--porcelain'])
    // Filter the LINES, not the whole listing: a repository that also has real
    // uncommitted changes is still dirty, and must keep saying so.
    return withoutContainerLines(container, out.split('\n')).some((line) => line.trim().length > 0)
  } catch {
    return false
  }
}

// --- shared change-reconciliation primitives ---
// `checkout` and the worktree dialog must reconcile uncommitted changes the same
// way, so the two command sequences live here once. Both throw on failure (the
// callers turn that into their own error shape).

// Stash tracked + untracked changes under a human-readable label.
//
// No container guard here, deliberately: git does not record an untracked
// nested repository at all, so a stash neither captures the container nor
// disturbs what is inside it (measured: `stash push -u` prints "Ignoring path
// .worktrees/me/" and the tree's files stay on disk). `commitWip` — which force
// feeds `add -A` — is the one that needs the check.
export async function stashChanges(root: string, label: string): Promise<void> {
  await runGit(root, ['stash', 'push', '-u', '-m', label])
}

// Commit everything as a throwaway WIP commit. `container` is the ONE thing
// `git add -A` must not be pointed at: a container git does not ignore is
// recorded as an embedded repository (a 160000 gitlink) that nobody can obtain,
// and `restore` cannot take it back. The dialog already blocks on a dirty tree,
// so this guard only fires for a caller that got here anyway.
//
// The check asks GIT, not the caller's snapshot, even when that snapshot says
// "already ignored": the snapshot is an `info` call old, the ignore can have
// been removed since, and this runs immediately before the one operation here
// that cannot be undone. A snapshot with no container name skips the check
// (nothing is known to protect — an older CLI, or not a repository).
export async function commitWip(root: string, container: WorktreeContainer = NO_CONTAINER): Promise<void> {
  if (container.name) {
    let ignored = false
    try {
      await runGit(root, ['check-ignore', '--no-index', '-q', `${container.name}/`])
      ignored = true
    } catch {
      ignored = false
    }
    const refusal = commitGuardRefusal(container, ignored)
    if (refusal) throw new Error(refusal)
  }
  await runGit(root, ['add', '-A'])
  await runGit(root, ['commit', '-m', 'chore: WIP'])
}

// Porcelain status lines with git's 2-char status code + separator stripped, so
// callers get plain paths. A directory that is not inside a repo (or where git
// is missing) yields []; a git failure propagates, because "clean" and "could
// not tell" must not look alike to a caller about to create a worktree.
export async function statusLines(cwd: string, container: WorktreeContainer = NO_CONTAINER): Promise<string[]> {
  const root = await repoRoot(cwd)
  if (!root) return []
  const out = await runGit(root, ['status', '--porcelain'])
  const lines = out
    .split('\n')
    .filter((line) => line.length > 3)
    .map((line) => line.slice(3))
  return withoutContainerLines(container, lines)
}

// Is this directory inside a git repository? The cheapest question here (one
// `rev-parse --show-toplevel`, no python): the sidebar's "new session in a git
// worktree" entry asks it per project, because a directory that is not a
// repository has no tree to make. Never throws — `repoRoot` swallows every
// failure (no git on PATH, a missing directory, a bare repo) into `null`.
export async function isRepo(cwd: string): Promise<boolean> {
  return (await repoRoot(cwd)) !== null
}

export async function gitInfo(cwd: string): Promise<GitInfo> {
  const root = await repoRoot(cwd)
  if (!root) return { inRepo: false, currentBranch: null, hasChanges: false, isWorktree: false, worktreeName: null }
  const info = await worktreeInfo(cwd)
  const branch = await currentBranch(root)
  const dirty = await hasChanges(root, containerFrom(info))
  // `is_worktree` is git's answer about THIS directory, and it is what turns the
  // branch pill into a disabled pill naming the tree (shared/worktree-rules.ts).
  // An unusable payload — no python, not a repository, an older CLI — leaves it
  // false, i.e. today's behaviour.
  const isWorktree = info.ok && info.is_worktree
  return {
    inRepo: true,
    currentBranch: branch,
    hasChanges: dirty,
    isWorktree,
    // The tree's own directory name, read off git's root (NOT off `cwd`, which
    // may be a subdirectory of the tree) — `me` for `<repo>/.worktrees/me`.
    worktreeName: isWorktree ? path.basename(root) : null,
  }
}

// Local branches of the tree `cwd` lives in. `opts.excludeWorktreeBranches`
// drops the ones a LINKED worktree currently holds: git refuses to check those
// out here ("already checked out at …"), and they belong to another session's
// tree. The rule is pure (shared/worktree-rules.ts) and WHICH branch each tree
// holds comes from the `cluxmate worktree list` CLI — the one implementation of
// that fact — so the desktop never parses `git worktree` output itself.
export async function gitBranches(
  cwd: string,
  opts: GitBranchOptions = {},
): Promise<GitBranchList> {
  const root = await repoRoot(cwd)
  if (!root) return { current: null, branches: [], hasChanges: false }
  const branch = await currentBranch(root)
  const dirty = await hasChanges(root, await containerFor(cwd))
  try {
    const out = await runGit(root, ['for-each-ref', '--format=%(refname:short)', 'refs/heads'])
    const branches = out.split('\n').map((b) => b.trim()).filter(Boolean)
    if (!opts.excludeWorktreeBranches) return { current: branch, branches, hasChanges: dirty }
    const trees = await worktreeList(cwd)
    return {
      current: branch,
      // A `list` that could not answer (no python, an older CLI) filters
      // nothing: the dropdown shows exactly what it showed before this rule.
      branches: trees.ok ? withoutWorktreeBranches(branches, trees.worktrees) : branches,
      hasChanges: dirty,
    }
  } catch {
    return { current: branch, branches: [], hasChanges: dirty }
  }
}

export async function checkout(
  cwd: string,
  branch: string,
  strategy: GitCheckoutStrategy,
): Promise<GitCheckoutResult> {
  const root = await repoRoot(cwd)
  if (!root) return { ok: false, message: 'Not a git repository' }

  const container = await containerFor(cwd)
  try {
    switch (strategy) {
      case 'stash':
        await stashChanges(root, `cluxmate: WIP before switch to ${branch}`)
        break
      case 'commit':
        await commitWip(root, container)
        break
      case 'discard':
        await runGit(root, ['reset', '--hard'])
        // -e protects CluxMate's per-project state from the untracked sweep:
        //   .cluxmate/  — permissions.json, mcp.json, skills.json
        //   AGENTS.md — the project-level durable-memory file the agent writes
        // The worktree container needs no `-e` and never did: `git clean -fd`
        // skips a nested repository outright ("Skipping repository
        // .worktrees/me", measured), and it skips IGNORED paths unless -x — so
        // a discard cannot take a session's checkout, or anything uncommitted
        // inside it, with it. Nothing here may add `-x`.
        await runGit(root, ['clean', '-fd', '-e', '.cluxmate', '-e', 'AGENTS.md'])
        break
      case 'direct':
        // Race guard: the renderer normally supplies an explicit strategy when
        // the tree is dirty; refuse a direct switch over uncommitted changes.
        if (await hasChanges(root, container)) {
          return { ok: false, message: 'Working tree has uncommitted changes' }
        }
        break
    }

    await runGit(root, ['checkout', branch])
    const current = await currentBranch(root)
    return { ok: true, branch: current ?? branch }
  } catch (e: any) {
    return { ok: false, message: e?.message || 'git checkout failed' }
  }
}

// ---- live branch-change watch ----
// The agent switches branches by running git in its own (sandboxed) shell, so the
// Python side can't reliably tell us; instead the main process watches the repo's
// .git directory and notifies the renderer the moment HEAD/state files change.
// Watching the PARENT directory (not .git/HEAD itself) matters: git replaces HEAD
// atomically via rename, which a direct file watcher misses on Windows.

let gitWatcher: fs.FSWatcher | null = null
let gitWatchRoot: string | null = null
let gitWatchTimer: ReturnType<typeof setTimeout> | null = null

export async function watchGit(cwd: string, onChange: (cwd: string) => void): Promise<void> {
  const root = await repoRoot(cwd)
  if (!root) { stopGitWatch(); return }
  const gitDir = path.join(root, '.git')
  // Already watching this repo's .git dir — nothing to do.
  if (gitWatcher && gitWatchRoot === gitDir) return
  stopGitWatch()
  try {
    gitWatcher = fs.watch(gitDir, () => {
      if (gitWatchTimer) clearTimeout(gitWatchTimer)
      gitWatchTimer = setTimeout(() => {
        gitWatchTimer = null
        onChange(cwd)
      }, 250)
    })
    gitWatcher.on('error', () => stopGitWatch())
    gitWatchRoot = gitDir
  } catch {
    stopGitWatch()
  }
}

export function stopGitWatch(): void {
  if (gitWatchTimer) { clearTimeout(gitWatchTimer); gitWatchTimer = null }
  if (gitWatcher) { gitWatcher.close(); gitWatcher = null }
  gitWatchRoot = null
}
