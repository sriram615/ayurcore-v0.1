'use client'

/**
 * "Ask about this patient" -- the foundation of the AI-2 question panel inside Patient History (DOCTOR accounts).
 *
 * The input, the Ask and Clear controls and the idle / loading / result / error states. It sends to POST /patients/{patient_pk}/ask
 * (lib/api.ts askPatient) and NOTHING ELSE: no request is made until the doctor asks or chooses a reading, no other patient data is
 * fetched, and the clinic and the patient's authority come from the session and the path. The reply is checked and reduced to what may be
 * shown by lib/ask-view.ts and drawn by components/ask-result.tsx; a reply that does not match the contract becomes a fixed message, never
 * a guess.
 *
 * Clarification: the doctor picks one of the readings the server offered; the request is built by lib/ask-state.ts chooseRequest -- the
 * ORIGINAL question plus that reading verbatim -- so this component never constructs a frame or a query. The panel is mounted by
 * PatientHistory with the patient's key, so choosing another patient starts it afresh.
 *
 * Keyboard focus is never lost and moves only to where the next action is: the question box stays focusable while a request runs (it is
 * read-only, not disabled, and Ask is aria-disabled rather than disabled); when a request finishes, focus moves to the first reading of a
 * clarification, to the answer or refusal (so it is read first and Shift+Tab returns to the question), or back to the question box after
 * a failure and after Clear.
 */

import { useEffect, useId, useReducer, useRef } from 'react'
import { Search, X } from 'lucide-react'

import { ApiCallError, askPatient } from '@/lib/api'
import { ASK_MAX, askReducer, canAsk, canClear, chooseRequest, initialAskState, trimmedQuestion } from '@/lib/ask-state'
import { toAskView } from '@/lib/ask-view'
import AskResult from '@/components/ask-result'
import type { AskRequest, LoginResponse, PatientSummary } from '@/lib/api-types'

const ASK_FAILED = "That question couldn't be answered right now. Try again."
const ASK_UNREADABLE = "That answer couldn't be shown. Try asking again."
const CHOICE_INVALID = 'That choice is no longer valid. Ask your question again.'
const ERROR_TEXT = { request: ASK_FAILED, unreadable: ASK_UNREADABLE, choice_invalid: CHOICE_INVALID } as const

type Props = { patient: PatientSummary; session: LoginResponse; onSessionExpired: () => void }

export default function PatientAsk({ patient, session, onSessionExpired }: Props) {
  const [state, dispatch] = useReducer(askReducer, initialAskState)
  const abortRef = useRef<AbortController | null>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const resultRef = useRef<HTMLDivElement>(null)
  const wasLoading = useRef(false)
  const headingId = useId()
  const inputId = useId()
  const loading = state.phase === 'loading'

  // When a request finishes, put focus where the next action is (see the header). Not on first render and not on edits.
  useEffect(() => {
    const finished = wasLoading.current && state.phase !== 'loading'
    wasLoading.current = state.phase === 'loading'
    if (!finished) return
    if (state.phase === 'error') inputRef.current?.focus()
    else if (state.view?.kind === 'clarify') resultRef.current?.querySelector<HTMLButtonElement>('button.ask-reading')?.focus()
    else resultRef.current?.focus()
  }, [state.phase, state.view])

  // Leaving the panel (another patient, Close, sign-out) abandons any request still in flight.
  useEffect(() => () => abortRef.current?.abort(), [])

  /** Sends one request and routes the reply; `choosing` marks the request that carries a chosen reading. */
  const send = (request: AskRequest, choosing: boolean, requestId: number) => {
    const controller = new AbortController()
    abortRef.current?.abort()
    abortRef.current = controller
    askPatient(patient.patient_pk, request, session.token, controller.signal)
      .then((response) => {
        if (controller.signal.aborted) return
        const view = toAskView(response, patient.patient_pk, choosing)
        dispatch(view ? { type: 'resolve', requestId, view } : { type: 'fail', requestId, kind: 'unreadable' })
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        if (err instanceof ApiCallError && err.status === 401) {
          onSessionExpired()
          return
        }
        // A chosen reading the server refuses (422) is no longer valid; anything else is a failure the doctor can retry.
        const refused = choosing && err instanceof ApiCallError && err.status === 422
        dispatch({ type: 'fail', requestId, kind: refused ? 'choice_invalid' : 'request' })
      })
  }

  const submit = () => {
    if (!canAsk(state)) return
    dispatch({ type: 'submit' })
    send({ question: trimmedQuestion(state) }, false, state.requestId + 1)
  }

  const choose = (index: number) => {
    const request = chooseRequest(state, index)
    if (!request) return
    dispatch({ type: 'choose', index })
    send(request, true, state.requestId + 1)
  }

  const clear = () => {
    abortRef.current?.abort()
    abortRef.current = null
    dispatch({ type: 'clear' })
    inputRef.current?.focus()
  }

  return <section className="ask-panel" aria-labelledby={headingId} aria-busy={loading}>
    <span className="panel-label" id={headingId}>ASK ABOUT THIS PATIENT</span>
    <p className="patients-hint">Questions are answered only from this patient’s recorded prescriptions and visits.</p>

    <form className="clinical-form ask-form" onSubmit={(e) => { e.preventDefault(); submit() }} noValidate>
      <label htmlFor={inputId}>
        <span className="visually-hidden">Your question about {patient.display_name}</span>
        <textarea
          id={inputId}
          ref={inputRef}
          className="ask-input"
          rows={2}
          value={state.question}
          maxLength={ASK_MAX}
          placeholder="Ask about what is recorded, e.g. when Ashwagandha was last prescribed"
          autoComplete="off"
          spellCheck={false}
          enterKeyHint="send"
          readOnly={loading}
          onChange={(e) => dispatch({ type: 'edit', question: e.target.value })}
          onKeyDown={(e) => {
            // Enter asks; Shift+Enter adds a line; Enter that confirms an IME composition (Malayalam, Manglish keyboards) must not send.
            if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault()
              submit()
            }
          }}
        />
      </label>
      <div className="ask-actions">
        <button type="submit" className="button" disabled={!canAsk(state) && !loading} aria-disabled={loading || undefined}><Search size={15} aria-hidden="true" /> {loading ? 'Asking…' : 'Ask'}</button>
        <button type="button" className="button button-outline" disabled={!canClear(state)} onClick={clear}><X size={15} aria-hidden="true" /> Clear</button>
      </div>
    </form>

    {state.phase === 'loading' && <p className="patients-hint" role="status">Looking through the record…</p>}
    {state.view && <AskResult ref={resultRef} view={state.view} asked={state.asked} onChoose={choose} busyIndex={state.choosing} disabled={loading} />}
    {state.phase === 'error' && state.errorKind && <div className="inline-error" role="alert">{ERROR_TEXT[state.errorKind]}</div>}
  </section>
}
