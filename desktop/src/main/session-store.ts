import Database from 'better-sqlite3'
import { createHash } from 'crypto'
import { join, basename, dirname } from 'path'
import { app } from 'electron'
import { unlinkSync, existsSync, mkdirSync, copyFileSync, writeFileSync, readFileSync, rmSync, realpathSync } from 'fs'
import type { SessionMeta, CreateSessionParams, GroupMeta } from '../shared/types'

let _db: Database.Database | null = null

function dbPath(): string {
  return join(app.getPath('home'), '.cluxmate', 'cluxmate.db')
}

function migrateDbIfNeeded(): void {
  const newPath = dbPath()
  if (existsSync(newPath)) return

  // The db has lived in two different locations historically:
  // 1. %APPDATA%/cluxmate-desktop/cluxmate.db  (original electron-builder name)
  // 2. %APPDATA%/cluxmate/cluxmate.db         (current app.getPath('userData'))
  // Check both so users upgrading from any old version keep their sessions.
  const candidates = [
    join(dirname(app.getPath('userData')), 'cluxmate-desktop', 'cluxmate.db'),
    join(app.getPath('userData'), 'cluxmate.db'),
  ]
  for (const oldPath of candidates) {
    if (existsSync(oldPath)) {
      mkdirSync(dirname(newPath), { recursive: true })
      copyFileSync(oldPath, newPath)
      return
    }
  }
}

function getDb(): Database.Database {
  if (!_db) {
    migrateDbIfNeeded()
    const newPath = dbPath()
    mkdirSync(dirname(newPath), { recursive: true })
    _db = new Database(newPath)
    _db.pragma('journal_mode = WAL')
    _db.exec(`
      CREATE TABLE IF NOT EXISTS sessions (
        id            TEXT PRIMARY KEY,
        title         TEXT NOT NULL DEFAULT 'New Session',
        provider      TEXT NOT NULL,
        model         TEXT NOT NULL,
        cwd           TEXT NOT NULL,
        created_at    TEXT NOT NULL,
        updated_at    TEXT NOT NULL,
        message_count INTEGER DEFAULT 0
      )
    `)
    // v2 schema: pin the config model entry id (+ api_type snapshot) per
    // session. Older DBs predate these columns — add them if missing. Existing
    // rows get NULL, and ensureBridge falls back to the active model for those.
    const cols = new Set(
      (_db.prepare('PRAGMA table_info(sessions)').all() as { name: string }[])
        .map((c) => c.name)
    )
    if (!cols.has('model_id')) _db.exec('ALTER TABLE sessions ADD COLUMN model_id TEXT')
    if (!cols.has('api_type')) _db.exec('ALTER TABLE sessions ADD COLUMN api_type TEXT')
    // Per-session reasoning-level selection (null → provider default).
    if (!cols.has('reasoning_effort')) _db.exec('ALTER TABLE sessions ADD COLUMN reasoning_effort TEXT')

    // v3 schema: groups table + group_id on sessions.
    // Migrate from the earlier "projects" naming if present.
    const existingTables = new Set(
      (_db.prepare("SELECT name FROM sqlite_master WHERE type='table'").all() as { name: string }[])
        .map((r) => r.name)
    )
    if (existingTables.has('projects') && !existingTables.has('groups')) {
      _db.exec('ALTER TABLE projects RENAME TO groups')
      existingTables.add('groups')
    }
    if (!existingTables.has('groups')) {
      _db.exec(`
        CREATE TABLE IF NOT EXISTS groups (
          id         TEXT PRIMARY KEY,
          name       TEXT NOT NULL,
          created_at TEXT NOT NULL,
          sort_order INTEGER NOT NULL DEFAULT 0
        )
      `)
    }
    const pcols = new Set(
      (_db.prepare('PRAGMA table_info(sessions)').all() as { name: string }[])
        .map((c) => c.name)
    )
    if (pcols.has('project_id') && !pcols.has('group_id')) {
      _db.exec('ALTER TABLE sessions RENAME COLUMN project_id TO group_id')
    }
    if (!pcols.has('group_id') && !pcols.has('project_id')) {
      _db.exec('ALTER TABLE sessions ADD COLUMN group_id TEXT REFERENCES groups(id) ON DELETE SET NULL')
    }
    // v4: mark auto-created groups so they can be cleaned up when empty
    const gcols = new Set(
      (_db.prepare('PRAGMA table_info(groups)').all() as { name: string }[])
        .map((c) => c.name)
    )
    if (!gcols.has('is_auto')) {
      _db.exec('ALTER TABLE groups ADD COLUMN is_auto INTEGER NOT NULL DEFAULT 0')
    }
    // v6: auto groups are keyed by their RESOLVED working directory (`path`),
    // not by name — two different directories that share a basename must not
    // collapse into one project. `name` stays as the display label.
    if (!gcols.has('path')) {
      _db.exec('ALTER TABLE groups ADD COLUMN path TEXT')
    }
    // Backfill `path` for pre-existing auto groups from their first session's
    // cwd. Idempotent: only rows still lacking a path are touched.
    {
      const stale = _db.prepare(
        'SELECT id FROM groups WHERE is_auto = 1 AND path IS NULL'
      ).all() as { id: string }[]
      for (const g of stale) {
        const sess = _db.prepare(
          'SELECT cwd FROM sessions WHERE group_id = ? ORDER BY updated_at ASC LIMIT 1'
        ).get(g.id) as { cwd: string } | undefined
        if (sess?.cwd) {
          let resolved = sess.cwd
          try { resolved = realpathSync(sess.cwd) } catch { /* keep raw */ }
          _db.prepare('UPDATE groups SET path = ? WHERE id = ?').run(resolved, g.id)
        }
      }
    }
    // v5: pin sessions to the top within their group
    if (!pcols.has('is_pinned')) {
      _db.exec('ALTER TABLE sessions ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0')
    }
  }
  return _db
}

function resolveCwd(cwd: string): string {
  try { return realpathSync(cwd) } catch { return cwd }
}

function _cwdGroupName(cwd: string): string | null {
  const base = basename(cwd)
  // Root paths yield empty basename — skip auto-grouping
  return base || null
}

function _ensureGroupForCwd(cwd: string): string | null {
  const resolved = resolveCwd(cwd)
  const name = _cwdGroupName(resolved)
  if (!name) return null
  const db = getDb()
  // Auto groups are keyed by their RESOLVED path (not name), so two different
  // directories that share a basename stay as separate projects.
  const existing = db.prepare(
    'SELECT id FROM groups WHERE is_auto = 1 AND path = ?'
  ).get(resolved) as { id: string } | undefined
  if (existing) return existing.id
  const id = crypto.randomUUID().replace(/-/g, '').slice(0, 12)
  const now = new Date().toISOString()
  const maxOrder = (db.prepare('SELECT COALESCE(MAX(sort_order), -1) FROM groups').get() as Record<string, number>)
  const order = Object.values(maxOrder)[0] + 1
  db.prepare(
    'INSERT INTO groups (id, name, created_at, sort_order, is_auto, path) VALUES (?, ?, ?, ?, 1, ?)'
  ).run(id, name, now, order, resolved)
  return id
}

function _cleanupAutoGroup(groupId: string | null) {
  if (!groupId) return
  const db = getDb()
  const cnt = (db.prepare('SELECT COUNT(*) as cnt FROM sessions WHERE group_id = ?').get(groupId) as { cnt: number }).cnt
  if (cnt === 0) {
    db.prepare('DELETE FROM groups WHERE id = ? AND is_auto = 1').run(groupId)
  }
}

// Sessions can store the same directory with different spellings (relative vs
// absolute, trailing separator, symlink). The shadow repo is keyed by the
// *resolved* path, so compare resolved paths rather than raw strings — matching
// the Python side's `_same_cwd`.
function sameCwd(a: string, b: string): boolean {
  if (!a || !b) return false
  try {
    return realpathSync(a) === realpathSync(b)
  } catch {
    return a === b
  }
}

// Delete the shadow-git checkpoint repo for a working directory, if one exists.
// The repo is keyed by sha1(resolve(cwd)) — the exact derivation the Python side
// uses (CheckpointManager) — and is shared across every session in that
// directory. Only call this once no session in the directory remains, or a still
// live session's stored checkpoint SHAs would dangle. A path that can no longer
// be resolved (realpathSync throws) is a safe no-op: the key can't be derived,
// and the 30-day retention sweep will reap any orphaned repo.
function deleteShadowRepoForCwd(cwd: string): void {
  try {
    const resolved = realpathSync(cwd)
    const digest = createHash('sha1').update(resolved).digest('hex')
    rmSync(join(app.getPath('home'), '.cluxmate', 'checkpoints', `${digest}.git`), {
      recursive: true, force: true, maxRetries: 3, retryDelay: 100,
    })
  } catch { /* directory no longer resolvable — leave the repo for retention */ }
}

export function listSessions(): SessionMeta[] {
  const db = getDb()
  return db.prepare(
    'SELECT * FROM sessions ORDER BY is_pinned DESC, updated_at DESC'
  ).all() as SessionMeta[]
}

export function createSession(params: CreateSessionParams): SessionMeta {
  const db = getDb()
  const id = crypto.randomUUID().replace(/-/g, '').slice(0, 12)
  const now = new Date().toISOString()
  const groupId = _ensureGroupForCwd(params.cwd)

  db.prepare(`
    INSERT INTO sessions (id, title, provider, model, model_id, api_type, cwd, created_at, updated_at, message_count, group_id)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
  `).run(id, params.title || 'New Session', params.provider, params.model, params.modelId, params.apiType, params.cwd, now, now, groupId)

  // Seed the JSONL event-log header so the Python agent's _load_or_create_log
  // finds an existing session to append to (rather than creating a fresh one).
  // `modelId` is the config entry id (SQLite-only); the header carries the
  // display provider/model/apiType, matching Python's SessionStore.create().
  const sessionsDir = join(app.getPath('home'), '.cluxmate', 'sessions')
  mkdirSync(sessionsDir, { recursive: true })
  writeFileSync(
    join(sessionsDir, `${id}.jsonl`),
    JSON.stringify({
      type: 'session', version: 0, id,
      createdAt: Date.now(),
      cwd: params.cwd,
      provider: params.provider,
      model: params.model,
      apiType: params.apiType,
    }) + '\n',
    'utf-8',
  )

  return db.prepare('SELECT * FROM sessions WHERE id = ?').get(id) as SessionMeta
}

export function getSession(id: string): SessionMeta | undefined {
  return getDb().prepare('SELECT * FROM sessions WHERE id = ?').get(id) as SessionMeta | undefined
}

export function updateSession(
  id: string,
  updates: Partial<Pick<SessionMeta, 'title' | 'message_count' | 'model_id' | 'api_type' | 'model' | 'provider' | 'reasoning_effort'>>
) {
  const sets: string[] = []
  const vals: unknown[] = []

  if (updates.title !== undefined) { sets.push('title = ?'); vals.push(updates.title) }
  if (updates.message_count !== undefined) { sets.push('message_count = ?'); vals.push(updates.message_count) }
  if (updates.model_id !== undefined) { sets.push('model_id = ?'); vals.push(updates.model_id) }
  if (updates.api_type !== undefined) { sets.push('api_type = ?'); vals.push(updates.api_type) }
  if (updates.model !== undefined) { sets.push('model = ?'); vals.push(updates.model) }
  if (updates.provider !== undefined) { sets.push('provider = ?'); vals.push(updates.provider) }
  if (updates.reasoning_effort !== undefined) { sets.push('reasoning_effort = ?'); vals.push(updates.reasoning_effort) }

  if (sets.length === 0) return

  sets.push("updated_at = ?")
  vals.push(new Date().toISOString())
  vals.push(id)

  getDb().prepare(`UPDATE sessions SET ${sets.join(', ')} WHERE id = ?`).run(...vals)
}

export function updateSessionCwd(id: string, cwd: string) {
  const db = getDb()
  const old = db.prepare('SELECT group_id FROM sessions WHERE id = ?').get(id) as { group_id: string | null } | undefined
  const oldGroupId = old?.group_id ?? null
  const newGroupId = _ensureGroupForCwd(cwd)

  db.prepare(
    'UPDATE sessions SET cwd = ?, group_id = ?, updated_at = ? WHERE id = ?'
  ).run(cwd, newGroupId, new Date().toISOString(), id)

  if (oldGroupId && oldGroupId !== newGroupId) {
    _cleanupAutoGroup(oldGroupId)
  }
}

export function renameSession(id: string, title: string) {
  getDb().prepare(
    'UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?'
  ).run(title, new Date().toISOString(), id)
}

export function createGroup(name: string): GroupMeta {
  const db = getDb()
  const id = crypto.randomUUID().replace(/-/g, '').slice(0, 12)
  const now = new Date().toISOString()
  const maxOrder = (db.prepare('SELECT COALESCE(MAX(sort_order), -1) FROM groups').get() as { [key: string]: number })
  const order = maxOrder ? Object.values(maxOrder)[0] + 1 : 0
  db.prepare(
    'INSERT INTO groups (id, name, created_at, sort_order) VALUES (?, ?, ?, ?)'
  ).run(id, name, now, order)
  return db.prepare('SELECT * FROM groups WHERE id = ?').get(id) as GroupMeta
}

export function deleteGroup(id: string) {
  const db = getDb()
  db.prepare('UPDATE sessions SET group_id = NULL WHERE group_id = ?').run(id)
  db.prepare('DELETE FROM groups WHERE id = ?').run(id)
}

export function listGroups(): GroupMeta[] {
  return getDb().prepare('SELECT * FROM groups ORDER BY sort_order ASC').all() as GroupMeta[]
}

export function renameGroup(id: string, name: string) {
  getDb().prepare('UPDATE groups SET name = ? WHERE id = ?').run(name, id)
}

export function getGroup(id: string): GroupMeta | undefined {
  return getDb().prepare('SELECT * FROM groups WHERE id = ?').get(id) as GroupMeta | undefined
}

export function moveSessionToGroup(sessionId: string, groupId: string | null) {
  const db = getDb()
  // Remember source group before moving so we can clean up if it's now empty
  const session = db.prepare('SELECT group_id FROM sessions WHERE id = ?').get(sessionId) as { group_id: string | null } | undefined
  const oldGroupId = session?.group_id ?? null

  if (groupId === null) {
    db.prepare('UPDATE sessions SET group_id = NULL, updated_at = ? WHERE id = ?')
      .run(new Date().toISOString(), sessionId)
  } else {
    db.prepare('UPDATE sessions SET group_id = ?, updated_at = ? WHERE id = ?')
      .run(groupId, new Date().toISOString(), sessionId)
  }

  // If the session left an auto-group and that group is now empty, remove it
  if (oldGroupId && oldGroupId !== groupId) {
    _cleanupAutoGroup(oldGroupId)
  }
}

// Move a session out of a user-created group back to the auto group for its
// working directory (its "project"). Recreates the auto group if it was cleaned
// up when the session left (e.g. it was the project's last session).
export function moveSessionToProject(sessionId: string) {
  const db = getDb()
  const session = db.prepare('SELECT cwd FROM sessions WHERE id = ?').get(sessionId) as { cwd: string } | undefined
  if (!session) return
  moveSessionToGroup(sessionId, _ensureGroupForCwd(session.cwd))
}

export function pinSession(id: string, pinned: boolean) {
  getDb().prepare('UPDATE sessions SET is_pinned = ?, updated_at = ? WHERE id = ?')
    .run(pinned ? 1 : 0, new Date().toISOString(), id)
}

export function deleteSession(id: string) {
  const db = getDb()
  const session = db.prepare('SELECT group_id, cwd FROM sessions WHERE id = ?').get(id) as { group_id: string | null, cwd: string } | undefined
  db.prepare('DELETE FROM sessions WHERE id = ?').run(id)
  // Clean up auto-group if this was its last session
  if (session) _cleanupAutoGroup(session.group_id)
  const base = join(app.getPath('home'), '.cluxmate', 'sessions')
  // Delete subagent logs first (each is its own <child>.jsonl reachable from the
  // parent's spawn events), then the parent JSONL + display transcript.
  for (const childId of subagentSessionIds(id, base)) {
    try { unlinkSync(join(base, `${childId}.jsonl`)) } catch { /* may not exist */ }
  }
  for (const f of [`${id}.jsonl`, `${id}.display.json`]) {
    try { unlinkSync(join(base, f)) } catch { /* may not exist */ }
  }
  // The shadow repo is per-directory and shared across sessions. Only once no
  // session in this directory remains are its checkpoint SHAs unreferenced and
  // the repo safe to remove; otherwise leave it (other sessions' undo anchors
  // depend on the unchanged history).
  if (session?.cwd) {
    const remaining = db.prepare('SELECT cwd FROM sessions').all() as { cwd: string }[]
    if (!remaining.some((r) => sameCwd(session!.cwd, r.cwd))) {
      deleteShadowRepoForCwd(session.cwd)
    }
  }
}

// Collect every subagent session id reachable from a session's append-only JSONL
// log, so deleting a session cascades to its subagent logs. Reads + parses the
// JSONL directly (no Python process required): each line is an event envelope
// `{seq, time, type, data, ...}`; a `subagent/spawn` event's `data.session_id`
// is the child log's filename under the same sessions dir.
function subagentSessionIds(id: string, base: string, seen: Set<string> = new Set()): string[] {
  const out: string[] = []
  let lines: string[]
  try {
    lines = readFileSync(join(base, `${id}.jsonl`), 'utf-8').split('\n')
  } catch {
    return out
  }
  for (const line of lines) {
    if (!line.trim()) continue
    let obj: any
    try { obj = JSON.parse(line) } catch { continue }
    if (obj.type !== 'subagent/spawn') continue
    const childId = obj.data?.session_id
    if (!childId || seen.has(childId)) continue
    seen.add(childId)
    out.push(childId)
    out.push(...subagentSessionIds(childId, base, seen))
  }
  return out
}
