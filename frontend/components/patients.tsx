'use client'

/**
 * Patients workspace (DOCTOR / ADMIN): search the clinic's patient registry,
 * open a patient's confirmed prescription history, register a new patient.
 *
 * Uses only GET /patients?q=, GET /patients/{patient_pk}/prescriptions and
 * POST /patients. The backend is authoritative for everything shown here:
 * which clinic (always the session's -- nothing on this screen sends or
 * displays a clinic_id), who may see it (backend/rbac.py), search ranking,
 * and history order (rendered exactly as returned, newest first). Phone
 * numbers are never displayed, even though registration accepts one.
 */

import { useEffect, useRef, useState } from 'react'
import { ChevronLeft, FileText, Leaf, RefreshCw, Search, UserPlus, X } from 'lucide-react'

import { ApiCallError, patientHistory, registerPatient, searchPatients } from '@/lib/api'
import PatientAsk from '@/components/patient-ask'
import ConsultationBrief from '@/components/consultation-brief'
import { formatRecordDate } from '@/lib/format'
import type { ApiOperation } from '@/lib/errors'
import { friendlyMessage } from '@/lib/errors'
import type { LoginResponse, PatientPrescriptionRecord, PatientSummary } from '@/lib/api-types'
import {
  PATIENT_ID_MAX,
  SEARCH_MAX,
  searchableQuery,
  validatePatientForm,
  type PatientFormValues,
} from '@/lib/patient-validation'

const SEARCH_DEBOUNCE_MS = 300
const HISTORY_PAGE_SIZE = 20
const PATIENT_ROLES = new Set(['DOCTOR', 'ADMIN'])
const UNREACHABLE = 'AyurCore can’t be reached. Check the clinic’s connection, or ask whoever set this up.'

type Props = { session: LoginResponse; onSessionExpired: () => void }

export { formatRecordDate }

/** Shared ApiCallError handling for this screen: a 401 ends the session
 *  (returns ''), anything else becomes a friendly message -- never a raw
 *  backend detail. */
function useErrorMessage(onSessionExpired: () => void) {
  return (err: unknown, operation: ApiOperation): string => {
    if (err instanceof ApiCallError) {
      if (err.status === 401) {
        onSessionExpired()
        return ''
      }
      return friendlyMessage(err, operation)
    }
    return UNREACHABLE
  }
}

export default function PatientsView({ session, onSessionExpired }: Props) {
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState<{
    status: 'idle' | 'loading' | 'done' | 'error'
    results: PatientSummary[]
    truncated: boolean
    error: string
    forQuery: string
  }>({ status: 'idle', results: [], truncated: false, error: '', forQuery: '' })
  const [selected, setSelected] = useState<PatientSummary | null>(null)
  const [registering, setRegistering] = useState(false)
  const [registerPrefill, setRegisterPrefill] = useState<Partial<PatientFormValues>>({})
  const historyRef = useRef<HTMLElement>(null)
  const errorMessage = useErrorMessage(onSessionExpired)

  const allowed = PATIENT_ROLES.has(session.role)

  // Debounced search; a newer keystroke aborts the older request so a slow
  // response can never overwrite a newer one.
  useEffect(() => {
    if (!allowed) return
    const q = searchableQuery(query)
    if (!q) {
      setSearch({ status: 'idle', results: [], truncated: false, error: '', forQuery: '' })
      return
    }
    const controller = new AbortController()
    const timer = setTimeout(() => {
      setSearch((s) => ({ ...s, status: 'loading', error: '' }))
      searchPatients(q, session.token, controller.signal)
        .then((resp) => {
          if (controller.signal.aborted) return
          setSearch({ status: 'done', results: resp.results, truncated: resp.truncated, error: '', forQuery: q })
        })
        .catch((err) => {
          if (controller.signal.aborted) return
          const message = errorMessage(err, 'patient_search')
          if (message) setSearch({ status: 'error', results: [], truncated: false, error: message, forQuery: q })
        })
    }, SEARCH_DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
      controller.abort()
    }
    // errorMessage is recreated each render but only closes over onSessionExpired.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, session.token, allowed])

  const select = (patient: PatientSummary) => {
    setSelected(patient)
    setRegistering(false)
    // On a narrow screen the history renders below the results. The jump is instant, not animated: with the Ask panel above the history the page
    // is tall enough for the scroll to actually run, and an animation would still be moving while the layout is read (tests, assistive tech).
    requestAnimationFrame(() => historyRef.current?.scrollIntoView?.({ behavior: 'auto', block: 'start' }))
  }

  const openRegistration = () => {
    const q = query.trim()
    // Prefill from what the doctor already typed: digits suggest an ID.
    setRegisterPrefill(q ? (/\d/.test(q) ? { clinicPatientId: q } : { displayName: q }) : {})
    setRegistering(true)
  }

  if (!allowed) {
    return <div>
      <PageHeaderLite />
      <div className="inventory-note" role="status">
        <span>Patient records are available to doctor and administrator accounts only.</span>
      </div>
    </div>
  }

  return <div className="patients-view">
    <PageHeaderLite action={!registering && <button type="button" className="button button-outline" onClick={openRegistration}><UserPlus size={16} /> Register patient</button>} />

    <div className="patients-layout">
      <section className="patients-search" aria-label="Find a patient">
        {registering ? (
          <RegisterPatientForm
            session={session}
            prefill={registerPrefill}
            onSessionExpired={onSessionExpired}
            onCancel={() => setRegistering(false)}
            onRegistered={(patient) => {
              setRegistering(false)
              // Show the new patient in the list and open their record.
              setQuery(patient.clinic_patient_id)
              select(patient)
            }}
          />
        ) : <>
          <label className="search-field patients-search-field">
            <Search size={16} aria-hidden="true" />
            <span className="visually-hidden">Search patients by name or patient ID</span>
            <input
              type="search"
              value={query}
              maxLength={SEARCH_MAX}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search by name or patient ID"
              autoComplete="off"
            />
            {query && <button type="button" className="search-clear" aria-label="Clear search" onClick={() => setQuery('')}><X size={14} /></button>}
          </label>

          <SearchResults
            query={query}
            search={search}
            selectedPk={selected?.patient_pk ?? null}
            onSelect={select}
            onRegister={openRegistration}
          />
        </>}
      </section>

      <section ref={historyRef} className="patients-history" aria-label="Patient history">
        {selected
          ? <PatientHistory key={selected.patient_pk} patient={selected} session={session} onSessionExpired={onSessionExpired} onClose={() => setSelected(null)} />
          : <div className="patients-placeholder"><FileText size={20} aria-hidden="true" /><p>Select a patient to see their prescription history.</p></div>}
      </section>
    </div>
  </div>
}

function PageHeaderLite({ action }: { action?: React.ReactNode }) {
  return <div className="page-header"><div><div className="section-kicker">WORKSPACE · PATIENTS</div><h1>Patients</h1><p>Find a patient by name or clinic patient ID and review their confirmed prescriptions.</p></div>{action}</div>
}

function SearchResults({ query, search, selectedPk, onSelect, onRegister }: {
  query: string
  search: { status: 'idle' | 'loading' | 'done' | 'error'; results: PatientSummary[]; truncated: boolean; error: string; forQuery: string }
  selectedPk: number | null
  onSelect: (p: PatientSummary) => void
  onRegister: () => void
}) {
  if (search.status === 'idle') {
    return <p className="patients-hint">{query.trim() ? 'Keep typing — at least 2 characters.' : 'Type at least 2 characters of a name or patient ID.'}</p>
  }
  if (search.status === 'loading') {
    return <p className="patients-hint" role="status">Searching…</p>
  }
  if (search.status === 'error') {
    return <div className="inline-error" role="alert">{search.error}</div>
  }
  if (search.results.length === 0) {
    return <div className="patients-empty" role="status">
      <p>No patients match “{search.forQuery}”.</p>
      <button type="button" className="button button-outline" onClick={onRegister}><UserPlus size={15} /> Register a new patient</button>
    </div>
  }
  return <>
    <p className="result-count" role="status">{search.results.length} {search.results.length === 1 ? 'patient' : 'patients'}{search.truncated ? ' — showing the first matches, refine your search to see others' : ''}</p>
    <ul className="patient-results" aria-label="Matching patients">
      {search.results.map((p) => (
        <li key={p.patient_pk}>
          <button type="button" className={`patient-result${selectedPk === p.patient_pk ? ' selected' : ''}`} aria-pressed={selectedPk === p.patient_pk} onClick={() => onSelect(p)}>
            <strong>{p.display_name}</strong>
            <span className="mono">{p.clinic_patient_id}</span>
          </button>
        </li>
      ))}
    </ul>
  </>
}

function PatientHistory({ patient, session, onSessionExpired, onClose }: { patient: PatientSummary; session: LoginResponse; onSessionExpired: () => void; onClose: () => void }) {
  const [records, setRecords] = useState<PatientPrescriptionRecord[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [status, setStatus] = useState<'loading' | 'done' | 'error' | 'loading-more'>('loading')
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const errorMessage = useErrorMessage(onSessionExpired)

  useEffect(() => {
    const controller = new AbortController()
    setStatus('loading')
    setError('')
    patientHistory(patient.patient_pk, session.token, { limit: HISTORY_PAGE_SIZE, offset: 0 }, controller.signal)
      .then((resp) => {
        if (controller.signal.aborted) return
        setRecords(resp.prescriptions)
        setHasMore(resp.has_more)
        setStatus('done')
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        const message = errorMessage(err, 'patient_history')
        if (message) {
          setError(message)
          setStatus('error')
        }
      })
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patient.patient_pk, session.token, attempt])

  const loadMore = () => {
    setStatus('loading-more')
    setError('')
    patientHistory(patient.patient_pk, session.token, { limit: HISTORY_PAGE_SIZE, offset: records.length })
      .then((resp) => {
        setRecords((prev) => [...prev, ...resp.prescriptions])
        setHasMore(resp.has_more)
        setStatus('done')
      })
      .catch((err) => {
        const message = errorMessage(err, 'patient_history')
        setStatus('done')
        if (message) setError(message)
      })
  }

  return <div className="patient-record">
    <div className="panel-heading">
      <div>
        <span className="panel-label">PATIENT</span>
        <h3>{patient.display_name}</h3>
        <span className="patient-record-id mono">ID {patient.clinic_patient_id}</span>
      </div>
      <button type="button" className="button button-outline patients-back" onClick={onClose}><ChevronLeft size={15} /> Close</button>
    </div>

    {/* AI-3 Consultation Brief: available to DOCTOR and ADMIN accounts (read-only cited facts). */}
    {(session.role === 'DOCTOR' || session.role === 'ADMIN') && (
      <ConsultationBrief patient={patient} session={session} onSessionExpired={onSessionExpired} />
    )}

    {/* Doctor accounts only; the backend is the authority and answers 403 to anyone else. Remounted with this panel (key = patient_pk),
        so a different patient always starts from an empty question. */}
    {session.role === 'DOCTOR' && <PatientAsk patient={patient} session={session} onSessionExpired={onSessionExpired} />}

    <div className="panel-label patient-record-kicker">PRESCRIPTION HISTORY · NEWEST FIRST</div>

    {status === 'loading' && <p className="patients-hint" role="status">Loading prescription history…</p>}
    {status === 'error' && <div className="patients-error">
      <div className="inline-error" role="alert">{error}</div>
      <button type="button" className="button button-outline" onClick={() => setAttempt((n) => n + 1)}><RefreshCw size={15} /> Try again</button>
    </div>}
    {(status === 'done' || status === 'loading-more') && records.length === 0 && <div className="patients-empty" role="status">
      <p>No confirmed prescriptions for this patient yet.</p>
      <small>Prescriptions confirmed before a patient was selected at consultation aren’t linked to a patient record, so they don’t appear here.</small>
    </div>}

    {records.length > 0 && <ol className="history-list" aria-label={`Prescriptions for ${patient.display_name}`}>
      {records.map((rx) => <li key={rx.prescription_id} className="history-item">
        <div className="history-meta">
          <strong>{formatRecordDate(rx.created_at)}</strong>
          <span className="mono">#{rx.prescription_id}</span>
          <span className="status-text"><i />{rx.status}</span>
        </div>
        {rx.dosha_imbalance !== null && rx.prescribed_herbs !== null ? <>
          <div className="history-dosha"><span>Dosha</span><strong>{rx.dosha_imbalance}</strong></div>
          <ul className="history-herbs" aria-label="Herbs">
            {rx.prescribed_herbs.map((h) => <li key={h.name}><span><Leaf size={13} aria-hidden="true" />{h.name}</span><strong>{h.grams} g</strong></li>)}
          </ul>
        </> : <p className="patients-hint">Details for this record couldn’t be displayed.</p>}
      </li>)}
    </ol>}

    {status !== 'error' && error && <div className="inline-error" role="alert">{error}</div>}
    {hasMore && status !== 'loading' && status !== 'error' && <button type="button" className="button button-outline history-more" disabled={status === 'loading-more'} onClick={loadMore}>
      {status === 'loading-more' ? 'Loading…' : 'Load older prescriptions'}
    </button>}
  </div>
}

function RegisterPatientForm({ session, prefill, onSessionExpired, onCancel, onRegistered }: {
  session: LoginResponse
  prefill: Partial<PatientFormValues>
  onSessionExpired: () => void
  onCancel: () => void
  onRegistered: (patient: PatientSummary) => void
}) {
  const [values, setValues] = useState<PatientFormValues>({
    clinicPatientId: prefill.clinicPatientId ?? '',
    displayName: prefill.displayName ?? '',
    phone: '',
  })
  const [fieldErrors, setFieldErrors] = useState<Partial<Record<keyof PatientFormValues, string>>>({})
  const [error, setError] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const errorMessage = useErrorMessage(onSessionExpired)

  const set = (key: keyof PatientFormValues) => (e: React.ChangeEvent<HTMLInputElement>) => {
    setValues((v) => ({ ...v, [key]: e.target.value }))
    setFieldErrors((f) => ({ ...f, [key]: undefined }))
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submitting) return
    setError('')
    const result = validatePatientForm(values)
    setFieldErrors(result.errors)
    if (!result.body) return
    setSubmitting(true)
    try {
      const created = await registerPatient(result.body, session.token)
      onRegistered({ patient_pk: created.patient_pk, clinic_patient_id: created.clinic_patient_id, display_name: created.display_name })
    } catch (err) {
      const message = errorMessage(err, 'patient_register')
      if (message) setError(message)
    } finally {
      setSubmitting(false)
    }
  }

  const field = (key: keyof PatientFormValues, label: string, extra: React.InputHTMLAttributes<HTMLInputElement> = {}) => {
    const errorId = `patient-${key}-error`
    return <label>
      {label}
      <input value={values[key]} onChange={set(key)} disabled={submitting} aria-invalid={Boolean(fieldErrors[key])} aria-describedby={fieldErrors[key] ? errorId : undefined} {...extra} />
      {fieldErrors[key] && <span id={errorId} className="field-error">{fieldErrors[key]}</span>}
    </label>
  }

  return <form className="clinical-form patient-register" onSubmit={submit} noValidate aria-label="Register a patient">
    <div>
      <span className="panel-label">NEW PATIENT</span>
      <h3 className="patient-register-title">Register a patient</h3>
      <p className="patients-hint">The patient is added to your clinic. The patient ID can’t be changed later.</p>
    </div>
    {field('clinicPatientId', `Clinic patient ID (OP / registration no., up to ${PATIENT_ID_MAX} characters)`, { placeholder: 'e.g. OP-1048', autoComplete: 'off' })}
    {field('displayName', 'Patient name', { placeholder: 'Full name', autoComplete: 'off' })}
    {field('phone', 'Phone (optional)', { placeholder: 'Optional', type: 'tel', autoComplete: 'off' })}
    {error && <div className="inline-error" role="alert">{error}</div>}
    <div className="edit-actions">
      <button type="button" className="button button-outline" onClick={onCancel} disabled={submitting}>Cancel</button>
      <button type="submit" className="button" disabled={submitting}>{submitting ? 'Registering…' : <><UserPlus size={15} /> Register patient</>}</button>
    </div>
  </form>
}
