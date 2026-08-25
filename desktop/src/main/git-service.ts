import { execFile } from 'child_process'
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
        await runGit(root, ['stash', 'push', '-u', '-m', `cluxmate: WIP before switch to ${branch}`])
        break
      case 'commit':
        await runGit(root, ['add', '-A'])
        await runGit(root, ['commit', '-m', 'chore: WIP'])
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
