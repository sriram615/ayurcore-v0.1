'use client'

/**
 * Dashboard (every role): a read-only overview built ONLY from endpoints that already exist.
 *
 *   GET /inventory       DOCTOR, ADMIN   herb count, in / out of stock, total grams
 *   GET /audit/export    ADMIN, AUDITOR  confirmed prescriptions, how many changed from the AI's proposal, recent records
 *   GET /audit/events    ADMIN, AUDITOR  event count, chain integrity, recent events
 *
 * Nothing here is estimated or invented. There is no endpoint that lists recent patients (patients are found by searching) and the
 * audit trail is closed to doctor accounts, so those panels say so instead of showing placeholder numbers. Each source loads and fails
 * independently; a failure never hides another source. The backend remains the authority for who may see what -- this screen only
 * decides which sources to ask for.
 */

import { useEffect, useState } from 'react'
import { ArrowRight, RefreshCw, ShieldCheck, Stethoscope, UsersRound } from 'lucide-react'

import { ApiCallError, auditEvents, auditExport, getInventory } from '@/lib/api'
import { ErrorCategory, translateApiError } from '@/lib/errors'
import type { AuditEventRecord, AuditEventsExportResponse, AuditExportResponse, AuditRecord, InventoryItem, LoginResponse } from '@/lib/api-types'
import { formatRecordDate } from '@/lib/format'

const RECENT = 5
const OUT_OF_STOCK_LISTED = 8
const LOAD_FAILED = "This summary couldn't be loaded. Try again."

type Props = { session: LoginResponse; onSessionExpired: () => void; onNavigate: (id: string) => void }
type Source<T> = { status: 'loading' | 'done' | 'error'; data: T | null; error: string }

/** A message for a failed read: the shared connection / access wording where it applies, otherwise a plain "couldn't load". Never a backend detail. */
function loadMessage(err: unknown): string {
  if (err instanceof ApiCallError) {
    const { category, message } = translateApiError(err, err.operation)
    if (category === ErrorCategory.CONNECTION || category === ErrorCategory.WRONG_ROLE) return message
  }
  return LOAD_FAILED
}

/** Loads one source when `enabled`; `reload` runs it again. A 401 ends the session; anything else is shown on that source only. */
export function useSource<T>(enabled: boolean, load: (signal: AbortSignal) => Promise<T>, onSessionExpired: () => void) {
  const [state, setState] = useState<Source<T>>({ status: enabled ? 'loading' : 'done', data: null, error: '' })
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    if (!enabled) return
    const controller = new AbortController()
    setState({ status: 'loading', data: null, error: '' })
    load(controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setState({ status: 'done', data, error: '' })
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        if (err instanceof ApiCallError && err.status === 401) {
          onSessionExpired()
          return
        }
        setState({ status: 'error', data: null, error: loadMessage(err) })
      })
    return () => controller.abort()
    // load closes over the session token only; onSessionExpired is stable enough for a one-shot fetch.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, attempt])

  return { ...state, reload: () => setAttempt((n) => n + 1) }
}

function newest<T extends { created_at: string }>(rows: T[], tiebreak: (row: T) => number): T[] {
  const time = (iso: string) => {
    const t = new Date(iso).getTime()
    return Number.isNaN(t) ? 0 : t
  }
  return [...rows].sort((a, b) => time(b.created_at) - time(a.created_at) || tiebreak(b) - tiebreak(a)).slice(0, RECENT)
}

function eventLabel(type: string): string {
  const words = type.toLowerCase().replace(/_/g, ' ').trim()
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : type
}

const grams = (n: number) => `${n.toLocaleString()} g`

export default function Dashboard({ session, onSessionExpired, onNavigate }: Props) {
  const canStock = session.role === 'DOCTOR' || session.role === 'ADMIN'
  const canAudit = session.role === 'ADMIN' || session.role === 'AUDITOR'
  const canPatients = canStock

  const stock = useSource<InventoryItem[]>(canStock, (signal) => getInventory(session.token, signal).then((r) => r.items), onSessionExpired)
  const records = useSource<AuditExportResponse>(canAudit, () => auditExport(session.token), onSessionExpired)
  const events = useSource<AuditEventsExportResponse>(canAudit, () => auditEvents(session.token), onSessionExpired)

  const action = session.role === 'DOCTOR'
    ? <button type="button" className="button" onClick={() => onNavigate('consultation')}><Stethoscope size={16} /> Start a consultation</button>
    : <button type="button" className="button button-outline" onClick={() => onNavigate('audit')}><ShieldCheck size={16} /> Open the audit trail</button>

  return <div className="dashboard">
    <div className="page-header">
      <div>
        <div className="section-kicker">{canAudit && !canStock ? 'OVERSIGHT · DASHBOARD' : 'WORKSPACE · DASHBOARD'}</div>
        <h1>Dashboard</h1>
        <p>{canAudit && !canStock
          ? 'Confirmed prescriptions and the audit log for your clinic, as recorded.'
          : 'Clinic stock and recent activity, as recorded. Nothing on this page is estimated.'}</p>
      </div>
      {action}
    </div>

    {canStock && <StockSection source={stock} />}
    {canAudit && <AuditSection records={records} events={events} />}

    <div className="dash-grid">
      {canAudit && <RecentPrescriptions source={records} />}
      {canAudit && <RecentEvents source={events} />}
      {canPatients && <section className="dash-panel" aria-labelledby="dash-patients-h">
        <div className="panel-heading"><div><span className="panel-label">PATIENTS</span><h3 id="dash-patients-h">Recent patients</h3></div></div>
        <div className="patients-empty" role="status">
          <p>No recent-patients list is available.</p>
          <small>Patients are found by searching the registry by name or clinic patient ID, so there is no list of recent ones to show here.</small>
          <button type="button" className="button button-outline" onClick={() => onNavigate('patients')}><UsersRound size={15} /> Find a patient <ArrowRight size={14} /></button>
        </div>
      </section>}
      {!canAudit && <section className="dash-panel" aria-labelledby="dash-ai-h">
        <div className="panel-heading"><div><span className="panel-label">AI ACTIVITY</span><h3 id="dash-ai-h">AI proposals and confirmations</h3></div></div>
        <div className="patients-empty" role="status">
          <p>No AI activity summary is available to this account.</p>
          <small>How often prescriptions were changed from the AI’s proposal is read from the audit trail, which is open to administrator and auditor accounts only.</small>
        </div>
      </section>}
    </div>
  </div>
}

export function Loading() {
  return <p className="patients-hint" role="status">Loading…</p>
}

export function SourceError({ error, onRetry }: { error: string; onRetry: () => void }) {
  return <div className="patients-error">
    <div className="inline-error" role="alert">{error}</div>
    <button type="button" className="button button-outline" onClick={onRetry}><RefreshCw size={15} /> Try again</button>
  </div>
}

function Stat({ label, value, note }: { label: string; value: string; note?: string }) {
  return <div className="dash-stat"><dt>{label}</dt><dd>{value}</dd>{note && <small>{note}</small>}</div>
}

function StockSection({ source }: { source: ReturnType<typeof useSource<InventoryItem[]>> }) {
  const items = source.data
  const out = items ? items.filter((i) => i.stock_grams <= 0) : []
  const inStock = items ? items.length - out.length : 0
  const total = items ? items.reduce((sum, i) => sum + Math.max(0, i.stock_grams), 0) : 0
  return <section className="dash-section" aria-labelledby="dash-stock-h">
    <div className="panel-heading"><div><span className="panel-label">STOCK</span><h3 id="dash-stock-h">Clinic stock</h3></div></div>
    {source.status === 'loading' && <Loading />}
    {source.status === 'error' && <SourceError error={source.error} onRetry={source.reload} />}
    {source.status === 'done' && items && (items.length === 0
      ? <div className="patients-empty" role="status"><p>No stock is recorded for this clinic.</p></div>
      : <>
        <dl className="dash-stats" aria-label="Stock summary">
          <Stat label="Herbs recorded" value={String(items.length)} />
          <Stat label="In stock" value={String(inStock)} />
          <Stat label="Out of stock" value={String(out.length)} />
          <Stat label="Total in stock" value={grams(total)} />
        </dl>
        {out.length > 0 && <div className="dash-out">
          <span className="inventory-status out-of-stock"><i aria-hidden="true" />Out of stock</span>
          <span>{out.slice(0, OUT_OF_STOCK_LISTED).map((i) => i.herb_name).join(', ')}{out.length > OUT_OF_STOCK_LISTED ? ` and ${out.length - OUT_OF_STOCK_LISTED} more` : ''}</span>
        </div>}
      </>)}
  </section>
}

function AuditSection({ records, events }: { records: ReturnType<typeof useSource<AuditExportResponse>>; events: ReturnType<typeof useSource<AuditEventsExportResponse>> }) {
  const r = records.data
  const e = events.data
  const loading = records.status === 'loading' || events.status === 'loading'
  return <section className="dash-section" aria-labelledby="dash-audit-h">
    <div className="panel-heading"><div><span className="panel-label">AI ACTIVITY</span><h3 id="dash-audit-h">AI proposals and confirmations</h3></div></div>
    {loading && <Loading />}
    {!loading && !r && !e && <p className="patients-hint">The summary appears once the records below load.</p>}
    {(r || e) && <dl className="dash-stats" aria-label="Audit summary">
      {r && <Stat label="Confirmed prescriptions" value={String(r.record_count)} />}
      {r && <Stat label="Changed from the AI’s proposal" value={String(r.changed_count)} />}
      {e && <Stat label="Audit events" value={String(e.event_count)} />}
      {e && <Stat label="Event chain" value={e.chain_valid ? 'Intact' : 'Violation'} note={e.chain_valid ? undefined : 'Integrity check failed'} />}
    </dl>}
    {e && !e.chain_valid && e.integrity_violations.length > 0 && <div className="inline-error" role="alert">{e.integrity_violations.join('; ')}</div>}
  </section>
}

function RecentPrescriptions({ source }: { source: ReturnType<typeof useSource<AuditExportResponse>> }) {
  const rows: AuditRecord[] = source.data ? newest(source.data.records, (x) => x.prescription_id) : []
  return <section className="dash-panel" aria-labelledby="dash-rx-h">
    <div className="panel-heading"><div><span className="panel-label">RECENT</span><h3 id="dash-rx-h">Recent prescriptions</h3></div></div>
    {source.status === 'loading' && <Loading />}
    {source.status === 'error' && <SourceError error={source.error} onRetry={source.reload} />}
    {source.status === 'done' && (rows.length === 0
      ? <div className="patients-empty" role="status"><p>No confirmed prescriptions yet.</p></div>
      : <div className="table-wrap"><table aria-label="Recent prescriptions"><thead><tr><th>Prescription</th><th>Date</th><th>Dosha</th><th>Changed</th></tr></thead><tbody>
        {rows.map((x) => <tr key={x.prescription_id}>
          <td><strong className="mono">#{x.prescription_id}</strong></td>
          <td>{formatRecordDate(x.created_at)}</td>
          <td>{x.confirmed_dosha_imbalance}</td>
          <td>{x.was_changed ? 'Yes' : 'No'}</td>
        </tr>)}
      </tbody></table></div>)}
  </section>
}

function RecentEvents({ source }: { source: ReturnType<typeof useSource<AuditEventsExportResponse>> }) {
  const rows: AuditEventRecord[] = source.data ? newest(source.data.events, (x) => x.event_id) : []
  return <section className="dash-panel" aria-labelledby="dash-events-h">
    <div className="panel-heading"><div><span className="panel-label">RECENT</span><h3 id="dash-events-h">Recent activity</h3></div></div>
    {source.status === 'loading' && <Loading />}
    {source.status === 'error' && <SourceError error={source.error} onRetry={source.reload} />}
    {source.status === 'done' && (rows.length === 0
      ? <div className="patients-empty" role="status"><p>No activity has been recorded yet.</p></div>
      : <div className="table-wrap"><table aria-label="Recent activity"><thead><tr><th>Event</th><th>Record</th><th>Date</th></tr></thead><tbody>
        {rows.map((x) => <tr key={x.event_id}>
          <td>{eventLabel(x.event_type)}</td>
          <td>{x.prescription_id ? <strong className="mono">#{x.prescription_id}</strong> : '—'}</td>
          <td>{formatRecordDate(x.created_at)}</td>
        </tr>)}
      </tbody></table></div>)}
  </section>
}
