'use client'

/**
 * Encounter Lifecycle Management (Phase 1A/1B, migrations 010 & 018).
 *
 * Provides doctors with the ability to:
 * 1. Open an encounter for a patient (with visit type, chief complaint, and vitals).
 * 2. See the active encounter clearly marked as "OPEN".
 * 3. Close the active encounter (terminal and audited).
 * 4. View past encounter history with clinical visit metadata.
 */

import { useEffect, useState } from 'react'
import { Activity, Calendar, Check, ChevronDown, ChevronUp, Clock, Plus, Stethoscope, X } from 'lucide-react'

import { ApiCallError, closeEncounter, listPatientEncounters, openPatientEncounter } from '@/lib/api'
import { friendlyMessage } from '@/lib/errors'
import { formatRecordDate } from '@/lib/format'
import type {
  EncounterCreateRequest,
  EncounterResponse,
  LoginResponse,
  PatientSummary,
  VisitType,
} from '@/lib/api-types'

// ---------------------------------------------------------------------
// Status Badge
// ---------------------------------------------------------------------

export function EncounterStatusBadge({ status }: { status: string }) {
  if (status === 'OPEN') {
    return (
      <span className="rx-pill rx-pill-open" title="Encounter is currently open">
        <span className="status-dot-pulse" aria-hidden="true" />
        OPEN
      </span>
    )
  }
  return (
    <span className="rx-pill rx-pill-closed" title="Encounter closed (terminal record)">
      CLOSED
    </span>
  )
}

// ---------------------------------------------------------------------
// Open Encounter Modal
// ---------------------------------------------------------------------

interface OpenEncounterModalProps {
  patient: PatientSummary
  session: LoginResponse
  onSessionExpired: () => void
  onSuccess: (encounter: EncounterResponse) => void
  onClose: () => void
}

export function OpenEncounterModal({
  patient,
  session,
  onSessionExpired,
  onSuccess,
  onClose,
}: OpenEncounterModalProps) {
  const [visitType, setVisitType] = useState<VisitType>('FIRST_VISIT')
  const [chiefComplaint, setChiefComplaint] = useState('')
  const [vitalsBp, setVitalsBp] = useState('')
  const [vitalsWeight, setVitalsWeight] = useState('')
  const [vitalsTemp, setVitalsTemp] = useState('')
  const [followUpDate, setFollowUpDate] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', handleKey)
    return () => window.removeEventListener('keydown', handleKey)
  }, [submitting, onClose])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submitting) return
    setError('')
    setSubmitting(true)

    // Build payload conforming to extra="forbid" and validation bounds
    const payload: EncounterCreateRequest = {}
    if (visitType) payload.visit_type = visitType
    if (chiefComplaint.trim()) payload.chief_complaint = chiefComplaint.trim()
    if (vitalsBp.trim()) payload.vitals_bp = vitalsBp.trim()

    const weightNum = parseFloat(vitalsWeight)
    if (!isNaN(weightNum) && weightNum > 0 && weightNum <= 300) {
      payload.vitals_weight_kg = weightNum
    }

    const tempNum = parseFloat(vitalsTemp)
    if (!isNaN(tempNum) && tempNum >= 30 && tempNum <= 45) {
      payload.vitals_temperature_c = tempNum
    }

    if (followUpDate.trim()) payload.follow_up_date = followUpDate.trim()

    try {
      const res = await openPatientEncounter(patient.patient_pk, session.token, payload)
      onSuccess(res)
      onClose()
    } catch (err) {
      if (err instanceof ApiCallError && err.status === 401) {
        onSessionExpired()
        return
      }
      setError(
        err instanceof ApiCallError
          ? friendlyMessage(err, 'encounter_open')
          : 'Could not open encounter.'
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="open-encounter-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose()
      }}
    >
      <div className="modal-card" style={{ maxWidth: 520 }}>
        <div className="modal-header">
          <div>
            <span className="panel-label">CLINICAL ENCOUNTER</span>
            <h3 id="open-encounter-title" style={{ margin: 0 }}>
              Open new encounter
            </h3>
            <small style={{ color: 'var(--muted)', fontSize: 'var(--fs-caption)' }}>
              {patient.display_name} · {patient.clinic_patient_id}
            </small>
          </div>
          <button
            type="button"
            className="search-clear"
            aria-label="Close"
            disabled={submitting}
            onClick={onClose}
          >
            <X size={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} className="modal-body" style={{ gap: 14 }}>
          {error && (
            <div className="inline-error" role="alert">
              {error}
            </div>
          )}

          <label>
            Visit type
            <select
              value={visitType}
              onChange={(e) => setVisitType(e.target.value as VisitType)}
              disabled={submitting}
            >
              <option value="FIRST_VISIT">First Visit (Initial Consultation)</option>
              <option value="FOLLOW_UP">Follow-up Visit</option>
            </select>
          </label>

          <label>
            Chief complaint & observations
            <textarea
              rows={3}
              placeholder="e.g. Chronic knee pain, disturbed sleep, digestive heaviness..."
              value={chiefComplaint}
              onChange={(e) => setChiefComplaint(e.target.value)}
              disabled={submitting}
              maxLength={500}
            />
          </label>

          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 10 }}>
            <label>
              Blood pressure
              <input
                type="text"
                placeholder="120/80"
                value={vitalsBp}
                onChange={(e) => setVitalsBp(e.target.value)}
                disabled={submitting}
                maxLength={16}
              />
            </label>
            <label>
              Weight (kg)
              <input
                type="number"
                step="0.1"
                min="1"
                max="300"
                placeholder="65.0"
                value={vitalsWeight}
                onChange={(e) => setVitalsWeight(e.target.value)}
                disabled={submitting}
              />
            </label>
            <label>
              Temp (°C)
              <input
                type="number"
                step="0.1"
                min="30"
                max="45"
                placeholder="37.0"
                value={vitalsTemp}
                onChange={(e) => setVitalsTemp(e.target.value)}
                disabled={submitting}
              />
            </label>
          </div>

          <label>
            Follow-up date (optional)
            <input
              type="date"
              value={followUpDate}
              onChange={(e) => setFollowUpDate(e.target.value)}
              disabled={submitting}
            />
          </label>

          <div className="modal-footer" style={{ marginTop: 8 }}>
            <button
              type="button"
              className="button button-outline"
              disabled={submitting}
              onClick={onClose}
            >
              Cancel
            </button>
            <button type="submit" className="button button-primary" disabled={submitting}>
              <Stethoscope size={15} />
              {submitting ? 'Opening…' : 'Open encounter'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------
// Patient Encounters Section
// ---------------------------------------------------------------------

interface PatientEncountersSectionProps {
  patient: PatientSummary
  session: LoginResponse
  onSessionExpired: () => void
}

export function PatientEncountersSection({
  patient,
  session,
  onSessionExpired,
}: PatientEncountersSectionProps) {
  const [encounters, setEncounters] = useState<EncounterResponse[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [actionError, setActionError] = useState('')
  const [modalOpen, setModalOpen] = useState(false)
  const [closingId, setClosingId] = useState<number | null>(null)
  const [showHistory, setShowHistory] = useState(false)

  const activeEncounter = encounters.find((e) => e.status === 'OPEN') || null
  const closedEncounters = encounters.filter((e) => e.status !== 'OPEN')

  const fetchEncounters = async (signal?: AbortSignal) => {
    setLoading(true)
    setError('')
    try {
      const res = await listPatientEncounters(
        patient.patient_pk,
        session.token,
        { limit: 50, offset: 0 },
        signal
      )
      if (!signal?.aborted) {
        setEncounters(res.encounters)
      }
    } catch (err) {
      if (signal?.aborted) return
      if (err instanceof ApiCallError && err.status === 401) {
        onSessionExpired()
        return
      }
      setError(
        err instanceof ApiCallError
          ? friendlyMessage(err, 'encounter_list')
          : 'Could not load patient encounters.'
      )
    } finally {
      if (!signal?.aborted) setLoading(false)
    }
  }

  useEffect(() => {
    const controller = new AbortController()
    fetchEncounters(controller.signal)
    return () => controller.abort()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [patient.patient_pk, session.token])

  const handleCloseEncounter = async (encounterPk: number) => {
    setClosingId(encounterPk)
    setActionError('')
    try {
      const updated = await closeEncounter(encounterPk, session.token)
      setEncounters((prev) =>
        prev.map((e) => (e.encounter_pk === encounterPk ? updated : e))
      )
    } catch (err) {
      if (err instanceof ApiCallError && err.status === 401) {
        onSessionExpired()
        return
      }
      setActionError(
        err instanceof ApiCallError
          ? friendlyMessage(err, 'encounter_close')
          : 'Could not close encounter.'
      )
    } finally {
      setClosingId(null)
    }
  }

  const handleEncounterOpened = (newEncounter: EncounterResponse) => {
    setEncounters((prev) => [newEncounter, ...prev])
  }

  return (
    <section className="encounter-section">
      <div className="encounter-heading">
        <div>
          <span className="panel-label">CLINICAL ENCOUNTER LIFECYCLE</span>
          <h4 style={{ margin: '4px 0 0', fontSize: 'var(--fs-base)', color: 'var(--primary)' }}>
            Patient Encounter
          </h4>
        </div>

        {session.role === 'DOCTOR' && !activeEncounter && (
          <button
            type="button"
            className="button button-primary"
            onClick={() => setModalOpen(true)}
            style={{ fontSize: 'var(--fs-small)' }}
          >
            <Stethoscope size={14} />
            Open encounter
          </button>
        )}
      </div>

      {actionError && (
        <div className="inline-error" role="alert" style={{ marginBottom: 12 }}>
          {actionError}
        </div>
      )}

      {loading && <p className="patients-hint" role="status">Loading encounters…</p>}

      {error && !loading && (
        <div className="patients-error">
          <div className="inline-error" role="alert">{error}</div>
          <button
            type="button"
            className="button button-outline"
            onClick={() => fetchEncounters()}
          >
            Try again
          </button>
        </div>
      )}

      {!loading && !error && (
        <>
          {/* Active Open Encounter Card */}
          {activeEncounter ? (
            <div className="encounter-active-card">
              <div className="encounter-active-header">
                <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                  <EncounterStatusBadge status="OPEN" />
                  <strong style={{ color: 'var(--primary)', fontSize: 'var(--fs-small)' }}>
                    {activeEncounter.visit_type === 'FIRST_VISIT'
                      ? 'First Visit Consultation'
                      : activeEncounter.visit_type === 'FOLLOW_UP'
                      ? 'Follow-up Consultation'
                      : 'Clinical Consultation'}
                  </strong>
                  <span className="mono" style={{ fontSize: 'var(--fs-caption)', color: 'var(--muted)' }}>
                    enc:{activeEncounter.encounter_ref}
                  </span>
                </div>

                {session.role === 'DOCTOR' && (
                  <button
                    type="button"
                    className="button button-outline"
                    disabled={closingId === activeEncounter.encounter_pk}
                    onClick={() => handleCloseEncounter(activeEncounter.encounter_pk)}
                    title="Closing seals this encounter into the audit trail"
                  >
                    <Check size={14} />
                    {closingId === activeEncounter.encounter_pk ? 'Closing…' : 'Close encounter'}
                  </button>
                )}
              </div>

              <div className="encounter-meta-grid">
                <div>
                  <span>Started</span>
                  <strong>{formatRecordDate(activeEncounter.created_at)}</strong>
                </div>
                {activeEncounter.follow_up_date && (
                  <div>
                    <span>Follow-up scheduled</span>
                    <strong>{activeEncounter.follow_up_date}</strong>
                  </div>
                )}
              </div>

              {activeEncounter.chief_complaint && (
                <div className="encounter-complaint">
                  <span>Chief complaint</span>
                  <p>{activeEncounter.chief_complaint}</p>
                </div>
              )}

              {(activeEncounter.vitals_bp ||
                activeEncounter.vitals_weight_kg ||
                activeEncounter.vitals_temperature_c) && (
                <div className="vitals-row">
                  <span className="vitals-label"><Activity size={12} /> Recorded vitals:</span>
                  {activeEncounter.vitals_bp && (
                    <span className="vitals-pill">BP {activeEncounter.vitals_bp}</span>
                  )}
                  {activeEncounter.vitals_weight_kg && (
                    <span className="vitals-pill">{activeEncounter.vitals_weight_kg} kg</span>
                  )}
                  {activeEncounter.vitals_temperature_c && (
                    <span className="vitals-pill">{activeEncounter.vitals_temperature_c} °C</span>
                  )}
                </div>
              )}
            </div>
          ) : (
            <div className="encounter-empty-box">
              <p>No active open encounter for this patient.</p>
              <small>
                Encounters track individual clinical visits. Open an encounter to record vitals and chief complaints before prescribing.
              </small>
            </div>
          )}

          {/* Past Closed Encounters Accordion */}
          {closedEncounters.length > 0 && (
            <div className="encounter-history-wrap">
              <button
                type="button"
                className="encounter-history-toggle"
                onClick={() => setShowHistory(!showHistory)}
                aria-expanded={showHistory}
              >
                <span>
                  Past encounters ({closedEncounters.length}{' '}
                  {closedEncounters.length === 1 ? 'visit' : 'visits'})
                </span>
                {showHistory ? <ChevronUp size={15} /> : <ChevronDown size={15} />}
              </button>

              {showHistory && (
                <ul className="encounter-history-list" aria-label="Closed encounters">
                  {closedEncounters.map((enc) => (
                    <li key={enc.encounter_pk} className="encounter-history-item">
                      <div className="encounter-history-item-top">
                        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                          <EncounterStatusBadge status="CLOSED" />
                          <strong>
                            {enc.visit_type === 'FIRST_VISIT'
                              ? 'First Visit'
                              : enc.visit_type === 'FOLLOW_UP'
                              ? 'Follow-up'
                              : 'Consultation'}
                          </strong>
                          <span className="mono" style={{ fontSize: 'var(--fs-caption)', color: 'var(--muted)' }}>
                            enc:{enc.encounter_ref}
                          </span>
                        </div>
                        <span style={{ fontSize: 'var(--fs-caption)', color: 'var(--muted)' }}>
                          {formatRecordDate(enc.created_at)}
                          {enc.closed_at ? ` · Closed ${formatRecordDate(enc.closed_at)}` : ''}
                        </span>
                      </div>

                      {enc.chief_complaint && (
                        <p className="encounter-history-complaint">{enc.chief_complaint}</p>
                      )}

                      {(enc.vitals_bp || enc.vitals_weight_kg || enc.vitals_temperature_c) && (
                        <div className="vitals-row" style={{ marginTop: 6 }}>
                          {enc.vitals_bp && <span className="vitals-pill">BP {enc.vitals_bp}</span>}
                          {enc.vitals_weight_kg && (
                            <span className="vitals-pill">{enc.vitals_weight_kg} kg</span>
                          )}
                          {enc.vitals_temperature_c && (
                            <span className="vitals-pill">{enc.vitals_temperature_c} °C</span>
                          )}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </>
      )}

      {modalOpen && (
        <OpenEncounterModal
          patient={patient}
          session={session}
          onSessionExpired={onSessionExpired}
          onSuccess={handleEncounterOpened}
          onClose={() => setModalOpen(false)}
        />
      )}
    </section>
  )
}
