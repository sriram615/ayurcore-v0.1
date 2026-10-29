'use client'

/**
 * AI-3 Consultation Brief: read-only, cited clinical pre-consultation summary.
 *
 * Calls GET /patients/{patient_pk}/brief (DOCTOR and ADMIN accounts).
 * Every fact is verified against confirmed records; no diagnostic inference
 * or treatment rationale is ever generated. Allows toggling between the
 * learned salience model (arm="learned") and the fixed template (arm="template").
 *
 * Uses existing tokens, typography, and UI patterns only -- no custom colors.
 */

import { useState } from 'react'
import { ChevronDown, ChevronUp, RefreshCw, Sparkles } from 'lucide-react'

import { ApiCallError, getPatientBrief } from '@/lib/api'
import { friendlyMessage } from '@/lib/errors'
import type { BriefResponse, LoginResponse, PatientSummary } from '@/lib/api-types'

type Props = {
  patient: PatientSummary
  session: LoginResponse
  onSessionExpired: () => void
}

export default function ConsultationBrief({ patient, session, onSessionExpired }: Props) {
  const [open, setOpen] = useState(false)
  const [arm, setArm] = useState<'learned' | 'template'>('learned')
  const [brief, setBrief] = useState<BriefResponse | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const fetchBrief = (selectedArm: 'learned' | 'template') => {
    setLoading(true)
    setError('')
    getPatientBrief(patient.patient_pk, session.token, selectedArm)
      .then((data) => {
        setBrief(data)
        setLoading(false)
      })
      .catch((err) => {
        if (err instanceof ApiCallError && err.status === 401) {
          onSessionExpired()
          return
        }
        setError(err instanceof ApiCallError ? friendlyMessage(err, 'patient_brief') : 'Could not load consultation brief.')
        setLoading(false)
      })
  }

  const handleOpen = () => {
    setOpen(true)
    if (!brief) {
      fetchBrief(arm)
    }
  }

  const handleArmChange = (newArm: 'learned' | 'template') => {
    setArm(newArm)
    fetchBrief(newArm)
  }

  return (
    <section className="ask-panel brief-panel" aria-label="Consultation brief">
      <div className="brief-header">
        <div>
          <span className="panel-label">AI-3 BRIEF</span>
          <p className="patients-hint">
            Deterministic summary verified against recorded history. Read-only, zero hallucination.
          </p>
        </div>

        <div className="brief-controls">
          {!open ? (
            <button
              type="button"
              className="button button-outline"
              onClick={handleOpen}
            >
              <Sparkles size={14} aria-hidden="true" /> View brief <ChevronDown size={14} aria-hidden="true" />
            </button>
          ) : (
            <>
              <div className="arm-toggle" role="group" aria-label="Ranking model">
                <button
                  type="button"
                  className={`arm-button ${arm === 'learned' ? 'active' : ''}`}
                  onClick={() => handleArmChange('learned')}
                  disabled={loading}
                >
                  Learned
                </button>
                <button
                  type="button"
                  className={`arm-button ${arm === 'template' ? 'active' : ''}`}
                  onClick={() => handleArmChange('template')}
                  disabled={loading}
                >
                  Template
                </button>
              </div>

              <button
                type="button"
                className="refresh-button"
                aria-label="Refresh consultation brief"
                onClick={() => fetchBrief(arm)}
                disabled={loading}
              >
                <RefreshCw size={13} className={loading ? 'spinning' : ''} aria-hidden="true" />
              </button>

              <button
                type="button"
                className="button button-outline"
                onClick={() => setOpen(false)}
                aria-label="Hide brief"
              >
                <ChevronUp size={14} aria-hidden="true" />
              </button>
            </>
          )}
        </div>
      </div>

      {open && (
        <>
          {loading && (
            <div className="brief-loading" role="status">
              <p className="patients-hint">Verifying and ranking facts from records…</p>
            </div>
          )}

          {error && (
            <div className="inline-error" role="alert">
              {error}
            </div>
          )}

          {!loading && !error && brief && (
            <div className="brief-content">
              {brief.caveat && (
                <div className="ask-caveat" role="note">
                  {brief.caveat}
                </div>
              )}

              {brief.items.length === 0 ? (
                <p className="patients-hint">No past prescriptions or visits recorded for this patient.</p>
              ) : (
                <ul className="brief-items" aria-label="Summary facts">
                  {brief.items.map((item, idx) => (
                    <li key={idx} className="brief-item">
                      <div className="brief-item-main">
                        <span className="brief-item-marker" aria-hidden="true">▪</span>
                        <span className="brief-item-text">{item.text}</span>
                      </div>

                      <div className="brief-item-meta">
                        <span className="brief-kind-pill">{item.kind}</span>
                        {item.records.map((rec) => (
                          <span key={rec} className="brief-cite-pill mono">
                            {rec}
                          </span>
                        ))}
                        {brief.arm === 'learned' && typeof item.score === 'number' && (
                          <span className="brief-score-pill mono" title={`Salience: ${item.score.toFixed(3)}`}>
                            salience {(item.score * 100).toFixed(0)}%
                          </span>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              )}

              <div className="brief-footer">
                <span className="mono">
                  as of {brief.as_of} · arm: {brief.arm}
                </span>
                {brief.omitted > 0 && <span>{brief.omitted} lower-ranked facts omitted</span>}
              </div>
            </div>
          )}
        </>
      )}
    </section>
  )
}
