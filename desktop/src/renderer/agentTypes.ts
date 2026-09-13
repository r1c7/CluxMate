// Lazily loaded subagent type catalog for the `task` approval card. Cached per
// session: definitions only change when the user edits a file, and the card can
// appear several times in one session.
import type { AgentTypeInfo } from '../shared/types'

const cache = new Map<string, Map<string, AgentTypeInfo>>()

export async function loadAgentTypes(sessionId: string): Promise<Map<string, AgentTypeInfo>> {
  const hit = cache.get(sessionId)
  if (hit) return hit
  try {
    const r = await window.electronAPI.getAgents(sessionId)
    const map = new Map((r.agents || []).map((a) => [a.slug, a]))
    cache.set(sessionId, map)
    return map
  } catch {
    // Cold bridge / unreadable files: the card falls back to the raw params.
    return new Map()
  }
}
