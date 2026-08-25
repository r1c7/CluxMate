import { app, BrowserWindow, Menu, nativeImage } from 'electron'
import { join } from 'path'
import { existsSync } from 'fs'
import { setupSecurity } from './security'
import { registerIpcHandlers, killAllBridges, stopIdleReaper } from './ipc-handlers'
import { createTray } from './tray'
import { IPC } from '../shared/ipc-channels'

function createWindow() {
  // Resolve window icon from icon.ico
  const iconPath = join(__dirname, '..', '..', 'resources', 'icon.ico')
  const win = new BrowserWindow({
    width: 1200,
    height: 800,
    // Frameless: the renderer draws its own title bar (icon + min/max/close)
    // so its height is fully controllable instead of the fixed native strip.
    frame: false,
    icon: existsSync(iconPath) ? nativeImage.createFromPath(iconPath) : undefined,
    webPreferences: {
      preload: join(__dirname, '../preload/index.mjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  })

  // Keep the renderer's maximize/restore icon in sync with the real state
  // (the user can also toggle it via Win+Up, snap layouts, double-click, …).
  win.on('maximize', () => win.webContents.send(IPC.WINDOW_MAXIMIZED_CHANGED, true))
  win.on('unmaximize', () => win.webContents.send(IPC.WINDOW_MAXIMIZED_CHANGED, false))

  // Ctrl+Shift+I / F12 → toggle DevTools (Menu.setApplicationMenu(null) nukes
  // Chromium's built-in shortcut, so wire it here).
  win.webContents.on('before-input-event', (_event, input) => {
    if (input.key === 'F12' || ((input.control || input.meta) && input.shift && input.key.toLowerCase() === 'i')) {
      win.webContents.toggleDevTools()
    }
  })

  if (process.env.ELECTRON_RENDERER_URL) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL)
  } else {
    win.loadFile(join(__dirname, '../renderer/index.html'))
  }
}

app.whenReady().then(() => {
  Menu.setApplicationMenu(null)
  setupSecurity()
  registerIpcHandlers()
  createWindow()
  createTray()
})
app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit() })
app.on('before-quit', () => { stopIdleReaper(); killAllBridges() })
