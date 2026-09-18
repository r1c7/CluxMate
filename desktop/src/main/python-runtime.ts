// The Python runtime facts of the main process: which interpreter runs the
// agent core, what environment it must see, and where a packaged app's bundled
// cluxmate source lives. Extracted verbatim from agent-bridge so that the
// transient `python -m cluxmate worktree …` client (main/worktree.ts) runs the
// SAME interpreter with the SAME environment as the long-lived stdio bridge —
// if the two drifted, the desktop could create worktrees with one Python and
// then run sessions with another.
//
// Reads electron (`app.isPackaged`, `app.getPath`, `process.resourcesPath`), so
// this is main-process only. The electron-free half that the renderer may also
// import lives in ../shared/worktree-rules.ts.

import { app } from 'electron'
import { delimiter, join } from 'path'
import { existsSync, readFileSync } from 'fs'

// `python` on Windows (the launcher installed by python.org is `python.exe`;
// `python3` there is often the Microsoft Store stub), `python3` elsewhere.
export function pythonCommand(): string {
  return process.platform === 'win32' ? 'python' : 'python3'
}

// Prefer the cluxmate source bundled with the app (electron-builder.yml
// extraResources → <resources>/cluxmate/) over whatever the user may have
// pip-installed, so the desktop shell and the agent core are version-locked
// to the same release. Third-party deps (openai/jinja2/httpx) still come from
// the user's Python env — full self-containment is the PyInstaller step, not
// this one. Returns the PYTHONPATH entry to prepend, or null when not packaged
// (dev runs against the source checkout via `python -m`).
export function bundledCluxmatePythonpath(): string | null {
  if (!app.isPackaged) return null
  const resources = process.resourcesPath
  if (existsSync(join(resources, 'cluxmate', '__init__.py'))) {
    return resources
  }
  // Defensive: allow the package to be nested one level deeper.
  if (existsSync(join(resources, 'cluxmate', 'cluxmate', '__init__.py'))) {
    return join(resources, 'cluxmate')
  }
  return null
}

// Read the user's bash/MCP sandbox toggle (~/.cluxmate/sandbox.json). False
// disables the OS shell sandbox by injecting CLUXMATE_BASH_SANDBOX=off into the
// agent's spawn env — the Python core honors that at build (sandbox_disabled_by_env).
// Default = enabled (sandbox on) when the file is absent or unreadable.
function bashSandboxEnabled(): boolean {
  try {
    const p = join(app.getPath('home'), '.cluxmate', 'sandbox.json')
    const data = JSON.parse(readFileSync(p, 'utf-8'))
    return data.bash_sandbox_enabled !== false
  } catch {
    return true
  }
}

// The environment every `python -m cluxmate …` child is spawned with. The
// bridge passed exactly this object to spawn() before the extraction, and its
// keys are load-bearing: UTF-8 stdio (the RPC framing and every tool result is
// JSON on a pipe), unbuffered output, and the sandbox opt-out the Python side
// reads at agent build time. `extra` is applied last so a caller can override
// an inherited value; the bridge and the worktree client both pass none.
export function agentEnv(extra?: Record<string, string>): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONIOENCODING: 'utf-8',
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
  }
  if (!bashSandboxEnabled()) {
    env.CLUXMATE_BASH_SANDBOX = 'off'
  }
  const bundled = bundledCluxmatePythonpath()
  if (bundled) {
    // Prepend the bundled core so it wins over any pip-installed cluxmate,
    // while still letting the user's env provide third-party deps.
    env.PYTHONPATH = env.PYTHONPATH
      ? `${bundled}${delimiter}${env.PYTHONPATH}`
      : bundled
  }
  if (extra) Object.assign(env, extra)
  return env
}
