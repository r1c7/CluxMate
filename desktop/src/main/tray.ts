import { Tray, Menu, nativeImage, app, BrowserWindow } from 'electron'
import { join } from 'path'
import { existsSync } from 'fs'

let tray: Tray | null = null

export function createTray() {
  // Resolve tray icon from icon.ico
  const devPath = join(__dirname, '..', '..', 'resources', 'icon.ico')
  const icon = existsSync(devPath)
    ? nativeImage.createFromPath(devPath).resize({ width: 16, height: 16 })
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
