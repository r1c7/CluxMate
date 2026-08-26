import { Tray, Menu, nativeImage, app, BrowserWindow } from 'electron'
import { resolveIconPath } from './icon'

let tray: Tray | null = null

export function createTray() {
  // Resolve the runtime tray icon (ico on Windows, png on macOS/Linux).
  const iconPath = resolveIconPath()
  const icon = iconPath
    ? nativeImage.createFromPath(iconPath).resize({ width: 16, height: 16 })
    : nativeImage.createEmpty()

  tray = new Tray(icon)

  const contextMenu = Menu.buildFromTemplate([
    {
      label: 'Show CluxMate',
      click: () => {
        const win = BrowserWindow.getAllWindows()[0]
        if (win) { win.show(); win.focus() }
      },
    },
    { type: 'separator' },
    {
      label: 'Quit',
      click: () => { app.quit() },
    },
  ])

  tray.setToolTip('CluxMate')
  tray.setContextMenu(contextMenu)

  tray.on('click', () => {
    const win = BrowserWindow.getAllWindows()[0]
    if (win) { win.show(); win.focus() }
  })
}
