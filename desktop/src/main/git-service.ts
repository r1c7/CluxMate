import { execFile } from 'child_process'
import * as fs from 'fs'
import * as path from 'path'
import type { GitInfo, GitBranchList, GitCheckoutStrategy, GitCheckoutResult } from '../shared/types'

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

async function hasChanges(root: string): Promise<boolean> {
  try {
    return (await runGit(root, ['status', '--porcelain'])).trim().length > 0
  } catch {
    return false
  }
}

// --- shared change-reconciliation primitives ---
// `checkout` and the worktree dialog must reconcile uncommitted changes the same
// way, so the two command sequences live here once. Both throw on failure (the
// callers turn that into their own error shape).

// Stash tracked + untracked changes under a human-readable label.
export async function stashChanges(root: string, label: string): Promise<void> {
  await runGit(root, ['stash', 'push', '-u', '-m', label])
}

// Commit everything as a throwaway WIP commit.
export async function commitWip(root: string): Promise<void> {
  await runGit(root, ['add', '-A'])
  await runGit(root, ['commit', '-m', 'chore: WIP'])
}

// Porcelain status lines with git's 2-char status code + separator stripped, so
// callers get plain paths. A directory that is not inside a repo (or where git
// is missing) yields []; a git failure propagates, because "clean" and "could
// not tell" must not look alike to a caller about to create a worktree.
export async function statusLines(cwd: string): Promise<string[]> {
  const root = await repoRoot(cwd)
  if (!root) return []
  const out = await runGit(root, ['status', '--porcelain'])
  return out
    .split('\n')
    .filter((line) => line.length > 3)
    .map((line) => line.slice(3))
}

export async function gitInfo(cwd: string): Promise<GitInfo> {
  const root = await repoRoot(cwd)
  if (!root) return { inRepo: false, currentBranch: null, hasChanges: false }
  const branch = await currentBranch(root)
  const dirty = await hasChanges(root)
  return { inRepo: true, currentBranch: branch, hasChanges: dirty }
}

export async function gitBranches(cwd: string): Promise<GitBranchList> {
  const root = await repoRoot(cwd)
  if (!root) return { current: null, branches: [], hasChanges: false }
  const branch = await currentBranch(root)
  const dirty = await hasChanges(root)
  try {
    const out = await runGit(root, ['for-each-ref', '--format=%(refname:short)', 'refs/heads'])
    const branches = out.split('\n').map((b) => b.trim()).filter(Boolean)
    return { current: branch, branches, hasChanges: dirty }
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

  try {
    switch (strategy) {
      case 'stash':
        await stashChanges(root, `cluxmate: WIP before switch to ${branch}`)
        break
      case 'commit':
        await commitWip(root)
        break
      case 'discard':
        await runGit(root, ['reset', '--hard'])
        // -e protects CluxMate's per-project state from the untracked sweep:
        //   .cluxmate/  — permissions.json, mcp.json, skills.json
        //   AGENTS.md — the project-level durable-memory file the agent writes
        await runGit(root, ['clean', '-fd', '-e', '.cluxmate', '-e', 'AGENTS.md'])
        break
      case 'direct':
        // Race guard: the renderer normally supplies an explicit strategy when
        // the tree is dirty; refuse a direct switch over uncommitted changes.
        if (await hasChanges(root)) {
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
