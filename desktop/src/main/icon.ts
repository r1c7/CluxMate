import { join } from 'path'
import { existsSync } from 'fs'

// Runtime window/tray icon. nativeImage loads PNG on macOS/Linux and ICO on
// Windows; the .icns is only for electron-builder's macOS *bundle* icon and is
// not used at runtime.
function iconFileName(): string {
  return process.platform === 'win32' ? 'icon.ico' : 'icon.png'
}

// Locate the runtime icon. Packaged apps carry it via extraResources in
// process.resourcesPath; in dev it sits two levels up from out/main.
export function resolveIconPath(): string | null {
  const name = iconFileName()
  const prod = join(process.resourcesPath, name)
  if (existsSync(prod)) return prod
  const dev = join(__dirname, '..', '..', 'resources', name)
  if (existsSync(dev)) return dev
  return null
}
