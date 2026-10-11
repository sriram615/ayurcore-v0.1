/**
 * State for the "Ask about this patient" panel -- a pure reducer, no React and no network.
 *
 * Phases: idle -> loading -> result | error. A response that arrives for a request that is no longer current (the doctor cleared the
 * panel, asked again or chose a reading) is ignored, so a slow answer can never appear under a different question. The state holds only
 * the AskView that lib/ask-view.ts validated; it never sees an unchecked response.
 *
 * Clarification: `asked` is the question the current result answers, kept apart from the editable text so that choosing a reading always
 * resubmits the ORIGINAL question even if the doctor has typed something else meanwhile. A reading can only be chosen by its index in the
 * clarification the server offered (chooseRequest), so the frontend never builds a frame or a query. While a chosen reading is pending the
 * clarification stays on screen; if that request fails the readings come back so the doctor can choose again, unless the server said the
 * choice is not valid any more, in which case the readings are dropped and the question must be asked again.
 */
import type { AskRequest } from './api-types'
import type { AskView } from './ask-view'

/** The API accepts at most 1000 characters; a textarea maxLength counts UTF-16 units, which is never more than characters, so this is safe. */
export const ASK_MAX = 1000

export type AskPhase = 'idle' | 'loading' | 'result' | 'error'

/** Why the panel is in the error phase: the request failed, the reply could not be trusted enough to show, or the chosen reading was refused. */
export type AskErrorKind = 'request' | 'unreadable' | 'choice_invalid'

export interface AskState {
  phase: AskPhase
  /** What is in the input box. */
  question: string
  /** The trimmed question the current request / result belongs to (null before the first ask and after Clear). */
  asked: string | null
  /** Increments on every submit, choice and clear; a response is accepted only for the current id. */
  requestId: number
  view: AskView | null
  /** Index of the reading whose answer is pending, or null. */
  choosing: number | null
  errorKind: AskErrorKind | null
}

export type AskAction =
  | { type: 'edit'; question: string }
  | { type: 'submit' }
  | { type: 'choose'; index: number }
  | { type: 'resolve'; requestId: number; view: AskView }
  | { type: 'fail'; requestId: number; kind: AskErrorKind }
  | { type: 'clear' }

export const initialAskState: AskState = { phase: 'idle', question: '', asked: null, requestId: 0, view: null, choosing: null, errorKind: null }

export const trimmedQuestion = (state: AskState) => state.question.trim()

/** A question can be sent when it has text and nothing is already in flight. */
export function canAsk(state: AskState): boolean {
  return state.phase !== 'loading' && trimmedQuestion(state).length > 0
}

/** Clear is offered whenever there is anything to clear. */
export function canClear(state: AskState): boolean {
  return state.phase !== 'idle' || state.question.length > 0
}

/** A reading can be chosen only from the clarification currently shown, by index, while nothing is in flight. */
export function canChoose(state: AskState, index: number): boolean {
  return (
    state.phase !== 'loading' &&
    state.phase !== 'idle' &&
    state.asked !== null &&
    state.view?.kind === 'clarify' &&
    Number.isInteger(index) &&
    index >= 0 &&
    index < state.view.options.length
  )
}

/** The request for choosing reading `index`: the original question and the reading exactly as the server offered it, or null. */
export function chooseRequest(state: AskState, index: number): AskRequest | null {
  if (!canChoose(state, index) || state.view?.kind !== 'clarify' || state.asked === null) return null
  const { frame, query } = state.view.options[index]
  return { question: state.asked, choice: { frame, query } }
}

export function askReducer(state: AskState, action: AskAction): AskState {
  switch (action.type) {
    case 'edit':
      return { ...state, question: action.question.slice(0, ASK_MAX) }
    case 'submit':
      return canAsk(state)
        ? { ...state, phase: 'loading', asked: trimmedQuestion(state), requestId: state.requestId + 1, view: null, choosing: null, errorKind: null }
        : state
    case 'choose':
      return canChoose(state, action.index)
        ? { ...state, phase: 'loading', requestId: state.requestId + 1, choosing: action.index, errorKind: null }
        : state
    case 'resolve': {
      if (state.phase !== 'loading' || action.requestId !== state.requestId) return state
      const chosen = state.choosing !== null && state.view?.kind === 'clarify' ? state.view.options[state.choosing]?.text ?? null : null
      const view = action.view.kind === 'answer' ? { ...action.view, clarifiedAs: chosen } : action.view
      return { ...state, phase: 'result', view, choosing: null, errorKind: null }
    }
    case 'fail': {
      if (state.phase !== 'loading' || action.requestId !== state.requestId) return state
      const keepReadings = state.choosing !== null && action.kind !== 'choice_invalid' && state.view?.kind === 'clarify'
      return { ...state, phase: 'error', view: keepReadings ? state.view : null, choosing: null, errorKind: action.kind }
    }
    case 'clear':
      return { phase: 'idle', question: '', asked: null, requestId: state.requestId + 1, view: null, choosing: null, errorKind: null }
  }
}
