// How the desktop must treat CluxMate's OWN worktree container in a `git
// status` listing.
//
// Why this exists: the container (`<repo>/.worktrees`, the name the CLI reports
// as `container`) is CluxMate's directory, never one of the user's uncommitted
// changes. `cluxmate/core/worktree.py` stated that in `_dirty_files` long ago,
// but only for its OWN `create` guard — the desktop's readers kept counting it,
// so on a repository whose ignore could not be written the "new worktree"
// dialog read `?? .worktrees/`, refused to create anything, and offered a
// "commit the changes" button that `git add -A`s an embedded repository into
// the user's repo as a 160000 gitlink. Nothing here is about SECURITY: the
// ignore itself, the WriteFence deny subtree and the shadow-repo exclude all
// live on the Python side. This module only stops the two sides from
// disagreeing about what "clean" means.
//
// Pure data logic (no electron, no fs, no child process) so desktop/tests can
// exercise it with the zero-dependency Node runner — the same convention as
// ./worktree-rules.ts. Who supplies the facts (the `cluxmate worktree info`
// client, src/main/worktree.ts) is the caller's business.

export interface WorktreeContainer {
  /**
   * The container directory's NAME, straight from `info.container` — never a
   * path, and never hardcoded here. `''` means "unknown" (an older CLI, or a
   * directory that is not a repository), which every rule below treats as
   * "nothing to filter".
   */
  name: string
  /**
   * `info.container_ignored`. `false` means git does NOT ignore the container,
   * so its rows must be KEPT: on such a repository that line is a real,
   * actionable change — the very thing the dialog's warning exists for.
   */
  ignored: boolean
}

/** What to assume when the CLI could not say (no python, no git, not a repo). */
export const NO_CONTAINER: WorktreeContainer = { name: '', ignored: false }

/** Should the container's rows be dropped from this listing? */
export function containerActive(container: WorktreeContainer): boolean {
  return container.ignored && container.name.length > 0
}

/**
 * Is `line` the container itself or something inside it?
 *
 * `line` is a path from `git status --porcelain` with git's 2-char status code
 * and separator already stripped, so it is REPO-ROOT relative and may use either
 * separator. The test is therefore on the name plus a `/`, never on the
 * container's absolute path: a sibling that merely shares a prefix
 * (`.worktrees-old`) is a different directory, and `worktree remove` may be
 * reading a listing taken INSIDE the container, where the name still refers to
 * the same directory. (`isInsideOrEqual` in src/main/worktree-guard.ts answers
 * the absolute-path question for the remove guard; this answers the
 * relative-listing one.)
 */
export function isContainerLine(container: WorktreeContainer, line: string): boolean {
  if (container.name.length === 0) return false
  const path = line.trim().replace(/\\/g, '/')
  return path === container.name || path.startsWith(`${container.name}/`)
}

/**
 * `lines` minus every container row — the user's own changes, and only those.
 * Returns a NEW array either way (a caller that mutates its own listing must
 * not reach into a shared one).
 */
export function withoutContainerLines(
  container: WorktreeContainer,
  lines: readonly string[],
): string[] {
  if (!containerActive(container)) return [...lines]
  return lines.filter((line) => !isContainerLine(container, line))
}

/**
 * The message to REFUSE a `git add -A` with, or `null` to go ahead.
 *
 * The decision behind `commitWip`'s guard, kept pure so it is testable without
 * a repository: committing an unignored container records every worktree as an
 * embedded repository (a 160000 gitlink) that nobody can obtain, and `restore`
 * cannot take it back.
 *
 * * an unknown name (`''`) ⇒ `null`: nothing is known to protect (an older CLI
 *   on PATH, or a directory that is not a repository);
 * * `gitSaysIgnored` is the answer GIT gives for `<name>/` immediately before
 *   the add — never the older snapshot from `info`, which cannot see an ignore
 *   that has been removed since. Note the TRAILING SLASH in that question: it
 *   is what makes git answer about the pattern while the directory does not
 *   exist yet (the Python `_container_ignored` documents the measurement).
 */
export function commitGuardRefusal(
  container: WorktreeContainer,
  gitSaysIgnored: boolean,
): string | null {
  const name = container.name
  if (!name) return null
  if (gitSaysIgnored) return null
  return (
    `refusing to commit: git does not ignore ${name}/, and committing it would record every ` +
    `worktree as an embedded repository. Add ${name}/ to .gitignore, or move that directory ` +
    `out of the project.`
  )
}
