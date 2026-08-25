import React, { useEffect, useState } from 'react'
import { useT } from '../useI18n'

// Custom title bar for the frameless window: app icon + title, a draggable
// middle region, and native-style minimize / maximize / close controls. Drawn
// in the renderer (instead of the OS frame) so its height is fully controllable.
export default function TitleBar({
  onOpenSettings,
  sidebarCollapsed,
  onToggleSidebar,
}: {
  onOpenSettings: () => void
  sidebarCollapsed: boolean
  onToggleSidebar: () => void
}) {
  const [maximized, setMaximized] = useState(false)
  const t = useT()

  useEffect(() => {
    // Seed from the real state, then follow it (Win+Up / snap / double-click all
    // toggle maximize outside this component).
    window.electronAPI.isWindowMaximized().then(setMaximized).catch(() => {})
    return window.electronAPI.onWindowMaximizedChanged(setMaximized)
  }, [])

  const btn = 'h-full w-11 flex items-center justify-center text-ink-soft hover:bg-sidebar-hover hover:text-ink transition-colors'

  return (
    <div className="h-10 bg-sidebar border-b border-surface-border flex items-center select-none flex-shrink-0">
      {/* App name (left) — part of the drag region, like the native bar. */}
      <div className="flex items-center pl-3 drag-region">
        <span className="text-base font-medium text-ink-soft">CluxMate</span>
      </div>

      {/* Sidebar collapse/expand — sits left of Settings, same distance from the
          app name as Settings used to be. */}
      <button
        onClick={onToggleSidebar}
        className="no-drag ml-1.5 h-full w-9 flex items-center justify-center text-ink-soft hover:text-ink hover:bg-sidebar-hover transition-colors"
        title={sidebarCollapsed ? t('titlebar.expandSidebar') : t('titlebar.collapseSidebar')}
      >
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect width="18" height="18" x="3" y="3" rx="2" />
          <path d="M9 3v18" />
          {sidebarCollapsed
            ? <path d="m14 9 3 3-3 3" />
            : <path d="m16 15-3-3 3-3" />}
        </svg>
      </button>

      {/* Settings entry — a gear icon so it reads as a control, distinct from
          the app-name text beside it. */}
      <button
        onClick={onOpenSettings}
        className="no-drag ml-0.5 h-full w-9 flex items-center justify-center text-ink-soft hover:text-ink hover:bg-sidebar-hover transition-colors"
        title={t('titlebar.settings')}
      >
        <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
          <circle cx="12" cy="12" r="3" />
        </svg>
      </button>

      {/* Draggable region */}
      <div className="flex-1 h-full drag-region" />

      {/* Window controls (right) */}
      <div className="flex items-center h-full no-drag">
        <button onClick={() => window.electronAPI.minimizeWindow()} className={btn} title={t('titlebar.minimize')}>
          <svg className="w-4 h-4" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round">
            <path d="M2 6h8" />
          </svg>
        </button>
        <button onClick={() => window.electronAPI.toggleMaximizeWindow()} className={btn} title={maximized ? t('titlebar.restore') : t('titlebar.maximize')}>
          {maximized ? (
            <svg className="w-4 h-4" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1" strokeLinejoin="round">
              <rect x="2.5" y="4.5" width="5" height="5" />
              <path d="M4.5 4.5V2.5h5v5h-2" />
            </svg>
          ) : (
            <svg className="w-4 h-4" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1" strokeLinejoin="round">
              <rect x="2.5" y="2.5" width="7" height="7" />
            </svg>
          )}
        </button>
        <button
          onClick={() => window.electronAPI.closeWindow()}
          className="h-full w-11 flex items-center justify-center text-ink-soft hover:bg-red-500 hover:text-white transition-colors"
          title={t('titlebar.close')}
        >
          <svg className="w-4 h-4" viewBox="0 0 12 12" fill="none" stroke="currentColor" strokeWidth="1" strokeLinecap="round">
            <path d="M2.5 2.5l7 7M9.5 2.5l-7 7" />
          </svg>
        </button>
      </div>
    </div>
  )
}
