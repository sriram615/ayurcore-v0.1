/**
 * Pure-function tests (no browser) for the Ask panel's state machine: a response is accepted only for the current request, clearing
 * abandons anything in flight, and a question can be sent only when it has text and nothing is pending.
 */
import { expect, test } from '@playwright/test'

import { ASK_MAX, askReducer, canAsk, canChoose, canClear, chooseRequest, initialAskState, type AskState } from '../lib/ask-state'
import type { AskView } from '../lib/ask-view'

const shown = (title: string) => ({ title, headline: null, summary: null, rows: [], items: [], compare: null, caveat: null })
const VIEW: AskView = { kind: 'answer', shown: shown('x'), incomplete: false, focusHerb: null, evidence: [], evidenceTotal: 0, clarifiedAs: null }
const typed = (q: string): AskState => askReducer(initialAskState, { type: 'edit', question: q })

test('a new panel is idle, empty, and cannot ask or clear', () => {
  expect(initialAskState).toEqual({ phase: 'idle', question: '', asked: null, requestId: 0, view: null, choosing: null, errorKind: null })
  expect(canAsk(initialAskState)).toBe(false)
  expect(canClear(initialAskState)).toBe(false)
})

test('only a question with text can be asked, and whitespace does not count', () => {
  expect(canAsk(typed('   \n\t '))).toBe(false)
  expect(canAsk(typed(' Neem count? '))).toBe(true)
  expect(askReducer(typed('   '), { type: 'submit' }).phase).toBe('idle')
})

test('submit moves to loading with a new request id, and a second submit while loading is ignored', () => {
  const loading = askReducer(typed('Neem count?'), { type: 'submit' })
  expect(loading).toMatchObject({ phase: 'loading', requestId: 1, view: null, asked: 'Neem count?' })
  expect(canAsk(loading)).toBe(false)
  expect(askReducer(loading, { type: 'submit' })).toBe(loading)
})

test('a response for the current request becomes the result; a stale one is ignored', () => {
  const loading = askReducer(typed('Neem count?'), { type: 'submit' })
  expect(askReducer(loading, { type: 'resolve', requestId: 1, view: VIEW })).toMatchObject({ phase: 'result', view: VIEW })
  expect(askReducer(loading, { type: 'resolve', requestId: 0, view: VIEW })).toBe(loading)
  expect(askReducer(loading, { type: 'fail', requestId: 99, kind: 'request' })).toBe(loading)
})

test('a failure for the current request is the error phase, remembers why, and keeps the question for another try', () => {
  const failed = askReducer(askReducer(typed('Neem count?'), { type: 'submit' }), { type: 'fail', requestId: 1, kind: 'request' })
  expect(failed).toMatchObject({ phase: 'error', question: 'Neem count?', view: null, errorKind: 'request' })
  expect(canAsk(failed)).toBe(true)
})

test('clearing returns to idle and invalidates anything in flight', () => {
  const loading = askReducer(typed('Neem count?'), { type: 'submit' })
  const cleared = askReducer(loading, { type: 'clear' })
  expect(cleared).toMatchObject({ phase: 'idle', question: '', view: null, errorKind: null })
  expect(askReducer(cleared, { type: 'resolve', requestId: 1, view: VIEW })).toBe(cleared)
  expect(canClear(loading)).toBe(true)
  expect(canClear(cleared)).toBe(false)
})

test('a response arriving after a result or an error changes nothing', () => {
  const done = askReducer(askReducer(typed('q'), { type: 'submit' }), { type: 'resolve', requestId: 1, view: VIEW })
  expect(askReducer(done, { type: 'resolve', requestId: 1, view: { ...VIEW, shown: shown('other') } })).toBe(done)
  expect(askReducer(done, { type: 'fail', requestId: 1, kind: 'request' })).toBe(done)
})

test('the question is capped at the API limit, counting astral characters safely', () => {
  expect(typed('n'.repeat(ASK_MAX + 50)).question).toHaveLength(ASK_MAX)
  expect(ASK_MAX).toBe(1000)
  expect(typed('മലയാളം Manglish ' + '𝟙'.repeat(10)).question).toContain('മലയാളം')
})

test('an unreadable reply is its own error kind, and the next submit clears it', () => {
  const unreadable = askReducer(askReducer(typed('Neem count?'), { type: 'submit' }), { type: 'fail', requestId: 1, kind: 'unreadable' })
  expect(unreadable).toMatchObject({ phase: 'error', errorKind: 'unreadable', view: null })
  expect(askReducer(unreadable, { type: 'submit' })).toMatchObject({ phase: 'loading', errorKind: null, view: null })
})

// --- clarification ------------------------------------------------------------------------

const R1 = { frame: 'COUNT_HERB', text: 'The number of prescriptions containing Neem.', query: { op: 'count', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' } }
const R2 = { frame: 'SUM_HERB', text: 'The total grams of Neem.', query: { op: 'sum', target: 'herb', herb: 'Neem', dosha: null, last_n: null, a: 'previous', b: 'last' } }
const CLARIFY: AskView = { kind: 'clarify', options: [R1, R2] }
/** A panel showing a clarification for "Neem count?" while the input box holds something else. */
function clarifying(typedAfter = 'Neem count?'): AskState {
  const shown = askReducer(askReducer(typed('  Neem count?  '), { type: 'submit' }), { type: 'resolve', requestId: 1, view: CLARIFY })
  return askReducer(shown, { type: 'edit', question: typedAfter })
}

test('the request for a chosen reading is the ORIGINAL trimmed question plus that reading verbatim, whatever is typed meanwhile', () => {
  for (const typedAfter of ['Neem count?', 'something else entirely', '', 'Brahmi grams?']) {
    const state = clarifying(typedAfter)
    expect(chooseRequest(state, 0)).toEqual({ question: 'Neem count?', choice: { frame: 'COUNT_HERB', query: R1.query } })
    expect(chooseRequest(state, 1)).toEqual({ question: 'Neem count?', choice: { frame: 'SUM_HERB', query: R2.query } })
  }
  expect(Object.keys(chooseRequest(clarifying(), 0) ?? {})).toEqual(['question', 'choice'])
})

test('only an index of a reading that was offered can be chosen; there is no way to supply a frame or a query', () => {
  const state = clarifying()
  for (const bad of [-1, 2, 3, 99, 0.5, NaN, Infinity]) {
    expect(canChoose(state, bad)).toBe(false)
    expect(chooseRequest(state, bad)).toBeNull()
    expect(askReducer(state, { type: 'choose', index: bad })).toBe(state)
  }
  expect(chooseRequest(initialAskState, 0)).toBeNull()
})

test('a reading cannot be chosen from anything but a shown clarification', () => {
  const answered = askReducer(askReducer(typed('q'), { type: 'submit' }), { type: 'resolve', requestId: 1, view: VIEW })
  const notice = askReducer(askReducer(typed('q'), { type: 'submit' }), { type: 'resolve', requestId: 1, view: { kind: 'notice', category: 'unsafe' } })
  const loading = askReducer(typed('q'), { type: 'submit' })
  for (const state of [initialAskState, answered, notice, loading, typed('q')]) expect(chooseRequest(state, 0)).toBeNull()
})

test('choosing goes to loading with a new request id, keeps the readings on screen, and a second choice meanwhile is ignored', () => {
  const state = clarifying()
  const loading = askReducer(state, { type: 'choose', index: 1 })
  expect(loading).toMatchObject({ phase: 'loading', requestId: 2, choosing: 1, view: CLARIFY, asked: 'Neem count?' })
  expect(askReducer(loading, { type: 'choose', index: 0 })).toBe(loading)
  expect(chooseRequest(loading, 0)).toBeNull()
  expect(canAsk(loading)).toBe(false)
})

test('the answer to a chosen reading records which reading it answered, and keeps the original question', () => {
  const loading = askReducer(clarifying('typed meanwhile'), { type: 'choose', index: 1 })
  const done = askReducer(loading, { type: 'resolve', requestId: 2, view: VIEW })
  expect(done).toMatchObject({ phase: 'result', choosing: null, asked: 'Neem count?', question: 'typed meanwhile' })
  expect(done.view).toMatchObject({ kind: 'answer', clarifiedAs: 'The total grams of Neem.' })
})

test('a failed choice brings the readings back so the doctor can choose again; a refused choice drops them', () => {
  const loading = askReducer(clarifying(), { type: 'choose', index: 0 })
  const failed = askReducer(loading, { type: 'fail', requestId: 2, kind: 'request' })
  expect(failed).toMatchObject({ phase: 'error', errorKind: 'request', choosing: null, view: CLARIFY, asked: 'Neem count?' })
  expect(chooseRequest(failed, 1)).toEqual({ question: 'Neem count?', choice: { frame: 'SUM_HERB', query: R2.query } })
  const unreadable = askReducer(loading, { type: 'fail', requestId: 2, kind: 'unreadable' })
  expect(unreadable).toMatchObject({ phase: 'error', errorKind: 'unreadable', view: CLARIFY })
  const refused = askReducer(loading, { type: 'fail', requestId: 2, kind: 'choice_invalid' })
  expect(refused).toMatchObject({ phase: 'error', errorKind: 'choice_invalid', view: null, asked: 'Neem count?' })
  expect(chooseRequest(refused, 0)).toBeNull()
})

test('a stale reply to a chosen reading is ignored after Clear, after a new question, and after another choice', () => {
  const loading = askReducer(clarifying(), { type: 'choose', index: 0 })
  const cleared = askReducer(loading, { type: 'clear' })
  expect(askReducer(cleared, { type: 'resolve', requestId: 2, view: VIEW })).toBe(cleared)
  expect(cleared).toMatchObject({ phase: 'idle', asked: null, view: null, choosing: null })
  const asked = askReducer(askReducer(cleared, { type: 'edit', question: 'new question' }), { type: 'submit' })
  expect(askReducer(asked, { type: 'resolve', requestId: 2, view: VIEW })).toBe(asked)     // the old id is not the current one
  expect(askReducer(loading, { type: 'resolve', requestId: 1, view: VIEW })).toBe(loading)
})

test('a new question replaces a clarification and starts from nothing', () => {
  const next = askReducer(askReducer(clarifying('Brahmi grams?'), { type: 'submit' }), { type: 'edit', question: 'Brahmi grams?' })
  expect(next).toMatchObject({ phase: 'loading', asked: 'Brahmi grams?', view: null, choosing: null })
})
