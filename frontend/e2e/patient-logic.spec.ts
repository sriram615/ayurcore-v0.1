/**
 * Pure-function tests (no browser): the client-side patient validation must
 * agree with the backend's rules, and the patient-specific error messages
 * must not change what any existing prescription operation shows.
 */
import { expect, test } from '@playwright/test'

import { translateApiError } from '../lib/errors'
import { searchableQuery, validatePatientForm } from '../lib/patient-validation'

test('valid input is trimmed into exactly the request body, phone omitted when blank', () => {
  expect(validatePatientForm({ clinicPatientId: '  OP-1 ', displayName: ' Asha  Menon ', phone: '   ' })).toEqual({
    errors: {},
    body: { clinic_patient_id: 'OP-1', display_name: 'Asha  Menon' },
  })
  expect(validatePatientForm({ clinicPatientId: 'OP-1', displayName: 'A', phone: ' 98470 ' }).body?.phone).toBe('98470')
})

test('patient ID bounds match the backend: 1-50 characters after trimming, no control characters', () => {
  expect(validatePatientForm({ clinicPatientId: '   ', displayName: 'A', phone: '' }).errors.clinicPatientId).toBeTruthy()
  expect(validatePatientForm({ clinicPatientId: 'x'.repeat(50), displayName: 'A', phone: '' }).body).toBeTruthy()
  expect(validatePatientForm({ clinicPatientId: 'x'.repeat(51), displayName: 'A', phone: '' }).errors.clinicPatientId).toBeTruthy()
  expect(validatePatientForm({ clinicPatientId: 'OP\t1', displayName: 'A', phone: '' }).errors.clinicPatientId).toBeTruthy()
  expect(validatePatientForm({ clinicPatientId: 'OP-1', displayName: 'A', phone: '9'.repeat(33) }).errors.phone).toBeTruthy()
  expect(validatePatientForm({ clinicPatientId: 'OP-1', displayName: 'n'.repeat(201), phone: '' }).errors.displayName).toBeTruthy()
})

test('lengths count characters like the backend, not UTF-16 units', () => {
  // 50 astral characters are 100 UTF-16 units but 50 characters -- valid on the server.
  expect(validatePatientForm({ clinicPatientId: '𝟙'.repeat(50), displayName: 'A', phone: '' }).body).toBeTruthy()
  expect(validatePatientForm({ clinicPatientId: 'OP-1', displayName: 'അ'.repeat(200), phone: '' }).body).toBeTruthy()
})

test('search queries must be 2-100 characters after trimming', () => {
  expect(searchableQuery(' a ')).toBeNull()
  expect(searchableQuery(' an ')).toBe('an')
  expect(searchableQuery('x'.repeat(100))).toBe('x'.repeat(100))
  expect(searchableQuery('x'.repeat(101))).toBeNull()
})

test('patient operations get patient-specific messages; raw backend detail never shown', () => {
  expect(translateApiError({ status: 409, detail: 'dup' }, 'patient_register').category).toBe('PATIENT_DUPLICATE')
  expect(translateApiError({ status: 422, detail: 'x' }, 'patient_register').message).toMatch(/patient ID/)
  expect(translateApiError({ status: 404, detail: 'Patient not found.' }, 'patient_history').category).toBe('PATIENT_NOT_FOUND')
  expect(translateApiError({ status: 422, detail: 'x' }, 'patient_search').message).toBe('Type at least 2 characters to search.')
  expect(translateApiError({ status: 403, detail: 'Forbidden' }, 'patient_search').category).toBe('WRONG_ROLE')
  expect(translateApiError({ status: 401, detail: 'x' }, 'patient_history').category).toBe('SESSION')
  expect(translateApiError({ status: 500, detail: 'Traceback ...' }, 'patient_register').message).not.toContain('Traceback')
})

test('existing prescription error messages are unchanged', () => {
  expect(translateApiError({ status: 409, detail: 'Insufficient stock for herb X' }, 'confirm').category).toBe('INSUFFICIENT_STOCK')
  expect(translateApiError({ status: 409, detail: 'something else' }, 'confirm').category).toBe('IDEMPOTENCY_CONFLICT')
  expect(translateApiError({ status: 422, detail: 'Invalid AI schema returned' }, 'preview').category).toBe('AI_INVALID_RESPONSE')
  expect(translateApiError({ status: 404, detail: 'Preview not found or expired.' }, 'confirm').category).toBe('PREVIEW_EXPIRED')
})
