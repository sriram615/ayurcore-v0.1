/**
 * Session persistence for the bearer token returned by POST /auth/login.
 *
 * The token itself is treated as the opaque string the API contract says
 * it is (AyurCore_API_Contract_v1.md §3: "not a JWT the frontend decodes
 * itself... treat the token as an opaque string to store and replay --
 * don't parse it").
 *
 * Two backends are used, chosen by the existing login screen's "Remember
 * this device" checkbox (previously present but inert in the v0 design;
 * this wires it to real, if modest, behavior rather than adding a new
 * control): sessionStorage by default (survives a reload within the same
 * tab -- so a doctor mid-consult isn't logged out by an accidental
 * refresh -- but clears when the tab/browser closes), or localStorage
 * when the doctor opts in to staying signed in on that device. Either
 * way, only ONE of the two ever holds a session at a time.
 */
import type { Role } from './api-types'

const STORAGE_KEY = 'ayurcore_session'

export interface StoredSession {
  token: string
  username: string
  clinic_id: string
  role: Role
  expires_at: string
}

function storageFor(remember: boolean): Storage | null {
  if (typeof window === 'undefined') return null
  return remember ? window.localStorage : window.sessionStorage
}

export function saveSession(session: StoredSession, remember: boolean = false): void {
  if (typeof window === 'undefined') return
  try {
    // Clear the other backend first so a session never lives in both at
    // once (e.g. logging in with "remember" off after previously
    // checking it on).
    const other = remember ? window.sessionStorage : window.localStorage
    other.removeItem(STORAGE_KEY)
    storageFor(remember)?.setItem(STORAGE_KEY, JSON.stringify(session))
  } catch {
    // Storage can throw in private-browsing / storage-restricted contexts.
    // Losing persistence is not fatal -- the session still works for the
    // current in-memory app lifetime, it just won't survive a reload.
  }
}

export function loadSession(): StoredSession | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(STORAGE_KEY) ?? window.localStorage.getItem(STORAGE_KEY)
    if (!raw) return null
    const parsed = JSON.parse(raw) as StoredSession
    if (!parsed.token || !parsed.role) return null
    // Client-side expiry check is a UX convenience only (avoids firing a
    // request we already know is stale) -- the backend's own session
    // table is the actual authority, and a 401 from any call still routes
    // through the same clearSession()+return-to-login path regardless.
    if (parsed.expires_at && new Date(parsed.expires_at).getTime() <= Date.now()) {
      clearSession()
      return null
    }
    return parsed
  } catch {
    return null
  }
}

export function clearSession(): void {
  if (typeof window === 'undefined') return
  try {
    window.sessionStorage.removeItem(STORAGE_KEY)
    window.localStorage.removeItem(STORAGE_KEY)
  } catch {
    // Ignore.
  }
}
