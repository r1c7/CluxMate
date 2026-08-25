import React, { useEffect, useState } from 'react'
import { useStore } from './stores'
import { loadSidebarWidth, saveSidebarWidth, clampSidebarWidth } from './layout'
import SessionList from './components/SessionList'
import ChatView from './components/ChatView'
import QuickNav from './components/QuickNav'
import WorkingDirBar from './components/WorkingDirBar'
import InputBox from './components/InputBox'
import QuestionCard from './components/QuestionCard'
import SettingsView from './components/SettingsView'
import AgentInspector from './components/AgentInspector'
import CheckpointTimeline from './components/CheckpointTimeline'
import DiffPanel from './components/DiffPanel'
import ContextViewer from './components/ContextViewer'
import ContextMenu from './components/ContextMenu'
import SkillsView from './components/SkillsView'
import McpView from './components/McpView'
import HooksView from './components/HooksView'
import Toast from './components/Toast'
import TitleBar from './components/TitleBar'
import { useT } from './useI18n'

function Fallback({ error }: { error: Error }) {
  const t = useT()
  return (
    <div className="h-screen bg-surface text-ink flex items-center justify-center">
      <div className="text-center">
        <h1 className="text-xl text-red-600 mb-2">{t('app.renderError')}</h1>
        <pre className="text-sm text-ink-soft max-w-lg">{error.message}</pre>
      </div>
    </div>
  )
}

class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  state = { error: null as Error | null }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() {
    if (this.state.error) return <Fallback error={this.state.error} />
    return this.props.children
  }
}

function AppInner() {
  const t = useT()
  const loadSessions = useStore((s) => s.loadSessions)
  const initConfig = useStore((s) => s.initConfig)
  const switchSession = useStore((s) => s.switchSession)
  const createSession = useStore((s) => s.createSession)
  const sessions = useStore((s) => s.sessions)
  const activeSessionId = useStore((s) => s.activeSessionId)
  const workingDir = useStore((s) => s.workingDir)
  const selectedAgent = useStore((s) => s.selectedAgent)
  const checkpointsOpen = useStore((s) => s.checkpointsOpen)
  const contextOpen = useStore((s) => s.contextOpen)
  const toggleContext = useStore((s) => s.toggleContext)
  const focusAgent = useStore((s) => s.focusAgent)
  const diffView = useStore((s) => s.diffView)
  const mainView = useStore((s) => s.mainView)
  const showChat = useStore((s) => s.showChat)
  const showSettings = useStore((s) => s.showSettings)
  const pendingQuestion = useStore((s) => s.pendingQuestion)
  const activeSessionTitle = sessions.find((s) => s.id === activeSessionId)?.title || t('chat.newSession')
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [ready, setReady] = useState(false)
  // Width of the right-side agent-tree panel, resizable by dragging its left edge.
  const [panelWidth, setPanelWidth] = useState(380)
  const [dragging, setDragging] = useState(false)
  // Left sidebar width, resizable by dragging its right edge (mirrors the right
  // dock's resize handle). Persisted to localStorage so it survives restarts.
  const [sidebarWidth, setSidebarWidth] = useState(loadSidebarWidth)
  const [resizingSidebar, setResizingSidebar] = useState(false)

  useEffect(() => {
    if (!dragging) return
    const MIN = 280, MAX = 820
    const onMove = (e: MouseEvent) => {
      // Panel is docked right, so its width is the distance from the cursor to
      // the window's right edge.
      const w = window.innerWidth - e.clientX
      setPanelWidth(Math.min(MAX, Math.max(MIN, w)))
    }
    const onUp = () => setDragging(false)
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    // Prevent text selection / show a resize cursor while dragging.
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    }
  }, [dragging])

  // Left-sidebar resize: width follows the cursor's X (the sidebar starts at the
  // window's left edge), clamped so it stays usable but can't crowd out the chat.
  // Persisted on mouseup so we write localStorage once per drag, not per mousemove.
  useEffect(() => {
    if (!resizingSidebar) return
    // Snapshot the width at drag start so a no-move mouseup persists it unchanged.
    let latest = sidebarWidth
    const onMove = (e: MouseEvent) => {
      latest = clampSidebarWidth(e.clientX)
      setSidebarWidth(latest)
    }
    const onUp = () => {
      setResizingSidebar(false)
      saveSidebarWidth(latest)
    }
    window.addEventListener('mousemove', onMove)
    window.addEventListener('mouseup', onUp)
    document.body.style.userSelect = 'none'
    document.body.style.cursor = 'col-resize'
    return () => {
      window.removeEventListener('mousemove', onMove)
      window.removeEventListener('mouseup', onUp)
      document.body.style.userSelect = ''
      document.body.style.cursor = ''
    }
  }, [resizingSidebar])

  useEffect(() => {
    Promise.all([loadSessions(), initConfig()])
      .then(() => setReady(true))
      .catch(() => setReady(true))
  }, [])

  useEffect(() => {
    if (!ready || !workingDir) return
    if (activeSessionId) return

    if (sessions.length > 0) {
      // Pick the most recently edited session that has any messages —
      // independently of pin / group sort order (listSessions puts pinned
      // sessions first, which would otherwise steal the default slot from a
      // newer unpinned session).
      let best = sessions.find((s) => s.message_count > 0)
      if (best) {
        for (const s of sessions) {
          if (s.message_count > 0 && new Date(s.updated_at).getTime() > new Date(best.updated_at).getTime()) {
            best = s
          }
        }
      }
      switchSession((best || sessions[0]).id)
    } else {
      createSession()
    }
  }, [ready, workingDir])

  // The right-side dock (Context History / agent tree / checkpoint timeline /
  // diff) is part of the chat view only. Skills, MCP, and Settings take over the
  // full width, covering the dock column too.
  const panelOpen = mainView === 'chat' && (!!diffView || !!selectedAgent || checkpointsOpen || contextOpen)

  return (
    <div className="h-screen flex flex-col bg-chat text-ink">
      <TitleBar
        onOpenSettings={() => (mainView === 'settings' ? showChat() : showSettings())}
        sidebarCollapsed={sidebarCollapsed}
        onToggleSidebar={() => setSidebarCollapsed((v) => !v)}
      />
      <div className="flex-1 flex overflow-hidden">
        {!sidebarCollapsed && mainView !== 'settings' && (
          <>
            <SessionList width={sidebarWidth} />
            <div
              onMouseDown={() => setResizingSidebar(true)}
              className={`w-1 flex-shrink-0 cursor-col-resize hover:bg-accent/50 transition-colors ${
                resizingSidebar ? 'bg-accent/60' : 'bg-surface-border'
              }`}
              title={t('app.dragResize')}
            />
          </>
        )}
        <div className="flex-1 flex flex-col min-w-0">
          {/* Chat header — shows the active session title on the left (truncated
              when long) and the Context History button on the right. Spans only
              the chat column, so the left sidebar sits flush under the title bar.
              Shown only in the chat view: Skills/MCP/Settings provide their own
              headers and cover this row. */}
          {mainView === 'chat' && (
          <div className="h-9 bg-sidebar border-b border-surface-border flex items-center gap-2 px-3 flex-shrink-0">
            {/* Quick "New Session" — only when the sidebar is collapsed, so the
                user can start a session without expanding the sidebar. */}
            {sidebarCollapsed && (
              <>
                <button
                  onClick={() => { createSession(); showChat() }}
                  className="flex-shrink-0 w-7 h-7 flex items-center justify-center rounded-md text-ink-soft hover:text-accent hover:bg-accent/10 transition-colors"
                  title={t('chat.newSession')}
                >
                  <svg className="w-4 h-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                    <path d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z" />
                  </svg>
                </button>
                <span aria-hidden className="w-px h-4 bg-surface-border flex-shrink-0" />
              </>
            )}
            <span
              className="flex-1 min-w-0 truncate text-sm font-medium text-ink-soft"
              title={activeSessionTitle}
            >
              {activeSessionTitle}
            </span>
            {/* Checkpoint entry hidden for now — the feature and its panel stay
                wired up, just no header button. Restore this button to re-expose. */}
            <button
              onClick={() => toggleContext()}
              className={`text-xs px-2.5 py-1 rounded-md font-semibold border transition-colors flex-shrink-0 ${
                contextOpen
                  ? 'bg-accent/20 text-accent border-accent/50'
                  : 'bg-accent/10 text-accent border-accent/30 hover:bg-accent/20 hover:border-accent/50'
              }`}
              title={t('chat.contextHistory')}
            >{t('chat.contextHistory')}</button>
          </div>
          )}

          {mainView === 'skills' ? (
            <SkillsView />
          ) : mainView === 'mcp' ? (
            <McpView />
          ) : mainView === 'hooks' ? (
            <HooksView />
          ) : mainView === 'settings' ? (
            <SettingsView />
          ) : (
            <>
              <div className="flex-1 flex relative min-h-0">
                <ChatView />
                {!focusAgent && <QuickNav />}
              </div>
              {!focusAgent && (pendingQuestion ? (
                <QuestionCard />
              ) : (
                <>
                  <WorkingDirBar />
                  <InputBox />
                </>
              ))}
            </>
          )}
        </div>
        {panelOpen && (
          <div
            onMouseDown={() => setDragging(true)}
            className={`w-1 flex-shrink-0 cursor-col-resize hover:bg-accent/50 transition-colors ${
              dragging ? 'bg-accent/60' : 'bg-surface-border'
            }`}
            title={t('app.dragResize')}
          />
        )}
        <div
          style={{ width: panelOpen ? panelWidth : 0 }}
          className={`flex-shrink-0 overflow-hidden ${dragging ? '' : 'transition-[width] duration-200'} ${
            panelOpen ? 'border-l border-surface-border' : ''
          }`}
        >
          {diffView ? <DiffPanel /> : selectedAgent ? <AgentInspector /> : checkpointsOpen ? <CheckpointTimeline /> : contextOpen ? <ContextViewer /> : null}
        </div>
      </div>

      <ContextMenu />
      <Toast />
    </div>
  )
}

export default function App() {
  return (
    <ErrorBoundary>
      <AppInner />
    </ErrorBoundary>
  )
}
