import { app, BrowserWindow } from 'electron'

// Taskbar attention signal ("flash this window") while a permission prompt or a
// question card is waiting for the user.
//
// The renderer owns *whether* something is waiting (it already derives the
// pending cards from its per-session state); this module owns *how* to signal it
// per platform and *when* to stop.
//
// shouldFlash = pending && !focused:
//   - a focused window needs no signal — the user is looking at the card;
//   - a blurred one does, even if it was focused earlier: the agent is still
//     blocked on the user, so the signal is re-armed when window focus is lost.

let win: BrowserWindow | null = null
let pending = false
let flashing = false
// macOS: dock.bounce() returns an id that must be handed back to cancelBounce().
let bounceId: number | null = null
// Windows: the native flash can stop on its own (notably while minimized), so
// re-assert it on a slow timer — flashFrame(true) is idempotent.
let reassertTimer: NodeJS.Timeout | null = null

const REASSERT_MS = 4000

export function attachAttention(browserWindow: BrowserWindow) {
  win = browserWindow
  win.on('focus', sync)
  win.on('blur', sync)
  win.on('closed', () => { win = null; stop() })
  sync()
}

// Reported by the renderer on every change of "a prompt is waiting" (gated by
// the user's notification preference on that side).
export function setAttention(flag: boolean) {
  pending = flag
  sync()
}

function sync() {
  const shouldFlash = pending && !!win && !win.isFocused()
  if (shouldFlash && !flashing) start()
  else if (!shouldFlash && flashing) stop()
}

function start() {
  if (!win) return
  flashing = true
  if (process.platform === 'darwin') {
    // 'critical' bounces until the app is activated or the request is canceled,
    // and — per Electron's docs — only while the app is NOT focused, which is
    // exactly when we get here.
    const id = app.dock.bounce('critical')
    bounceId = id >= 0 ? id : null
  } else {
    win.flashFrame(true)
    if (process.platform === 'win32') {
      reassertTimer = setInterval(() => { win?.flashFrame(true) }, REASSERT_MS)
    }
  }
}

function stop() {
  if (reassertTimer) { clearInterval(reassertTimer); reassertTimer = null }
  if (process.platform === 'darwin') {
    if (bounceId !== null) { app.dock.cancelBounce(bounceId); bounceId = null }
  } else {
    win?.flashFrame(false)
  }
  flashing = false
}
