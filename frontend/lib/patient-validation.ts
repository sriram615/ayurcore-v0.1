/**
 * Client-side mirror of the backend's patient-registry input rules
 * (backend/schemas.py PatientCreateRequest, and the GET /patients `q`
 * bounds in backend/main.py). This only gives the doctor faster feedback --
 * the backend re-validates everything and remains authoritative.
 *
 * Lengths are counted in characters (code points), the same way Pydantic
 * counts them, not UTF-16 units: a Malayalam name must get the same
 * verdict here as on the server.
 */

export const PATIENT_ID_MAX = 50
export const PATIENT_NAME_MAX = 200
export const PATIENT_PHONE_MAX = 32
export const SEARCH_MIN = 2
export const SEARCH_MAX = 100

// Python's str.strip() default whitespace, closely enough for form input.
const trim = (value: string) => value.replace(/^\s+|\s+$/gu, '')
const charCount = (value: string) => Array.from(value).length
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/

export interface PatientFormValues {
  clinicPatientId: string
  displayName: string
  phone: string
  dateOfBirth?: string
  gender?: string
  prakriti?: string
  addressLine?: string
  city?: string
  state?: string
  pincode?: string
  allergies?: string
}

export interface PatientFormResult {
  errors: Partial<Record<keyof PatientFormValues, string>>
  /** Present only when there are no errors: the exact trimmed body to send. */
  body?: {
    clinic_patient_id: string
    display_name: string
    phone?: string
    date_of_birth?: string
    gender?: string
    address_line?: string
    city?: string
    state?: string
    pincode?: string
    prakriti?: string
    allergies?: string
  }
}

export function validatePatientForm(values: PatientFormValues): PatientFormResult {
  const id = trim(values.clinicPatientId)
  const name = trim(values.displayName)
  const phone = trim(values.phone)
  const dob = values.dateOfBirth ? trim(values.dateOfBirth) : ''
  const gender = values.gender ? trim(values.gender) : ''
  const prakriti = values.prakriti ? trim(values.prakriti) : ''
  const address = values.addressLine ? trim(values.addressLine) : ''
  const city = values.city ? trim(values.city) : ''
  const state = values.state ? trim(values.state) : ''
  const pincode = values.pincode ? trim(values.pincode) : ''
  const allergies = values.allergies ? trim(values.allergies) : ''
  const errors: PatientFormResult['errors'] = {}

  if (!id) errors.clinicPatientId = 'Enter the clinic patient ID (OP / registration number).'
  else if (charCount(id) > PATIENT_ID_MAX) errors.clinicPatientId = `Patient ID can be at most ${PATIENT_ID_MAX} characters.`
  else if (CONTROL_CHARS.test(id)) errors.clinicPatientId = 'Patient ID cannot contain line breaks or control characters.'

  if (!name) errors.displayName = "Enter the patient's name."
  else if (charCount(name) > PATIENT_NAME_MAX) errors.displayName = `Name can be at most ${PATIENT_NAME_MAX} characters.`

  if (phone && charCount(phone) > PATIENT_PHONE_MAX) errors.phone = `Phone can be at most ${PATIENT_PHONE_MAX} characters.`

  if (pincode && !/^\d{6}$/.test(pincode)) errors.pincode = 'Pincode must be a 6-digit postal code.'

  if (Object.keys(errors).length > 0) return { errors }
  return {
    errors,
    body: {
      clinic_patient_id: id,
      display_name: name,
      ...(phone ? { phone } : {}),
      ...(dob ? { date_of_birth: dob } : {}),
      ...(gender ? { gender } : {}),
      ...(prakriti ? { prakriti } : {}),
      ...(address ? { address_line: address } : {}),
      ...(city ? { city } : {}),
      ...(state ? { state } : {}),
      ...(pincode ? { pincode } : {}),
      ...(allergies ? { allergies } : {}),
    },
  }
}

/** The trimmed query if it is searchable, otherwise null. */
export function searchableQuery(raw: string): string | null {
  const q = trim(raw)
  const n = charCount(q)
  return n >= SEARCH_MIN && n <= SEARCH_MAX ? q : null
}
