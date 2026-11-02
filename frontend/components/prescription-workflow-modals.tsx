'use client'

import React, { useState, useEffect } from 'react'
import {
  Check,
  CreditCard,
  FileText,
  Leaf,
  PackageCheck,
  Printer,
  RefreshCw,
  X,
} from 'lucide-react'

import {
  dispensePrescription,
  getInvoice,
  recordPayment,
  ApiCallError,
} from '@/lib/api'
import { friendlyMessage } from '@/lib/errors'
import { formatRecordDate } from '@/lib/format'
import type {
  InvoiceResponse,
  LoginResponse,
  PatientPrescriptionRecord,
  PaymentMethod,
  PaymentRequest,
  PaymentResponse,
  PaymentStatus,
} from '@/lib/api-types'

// ---------------------------------------------------------------------
// Badges: Dispense & Payment Status
// ---------------------------------------------------------------------

export function DispenseBadge({
  dispensedAt,
}: {
  dispensedAt?: string | null
}) {
  if (dispensedAt) {
    return (
      <span className="rx-pill rx-pill-dispensed" title={`Dispensed at ${dispensedAt}`}>
        <i aria-hidden="true" /> Dispensed
      </span>
    )
  }
  return (
    <span className="rx-pill rx-pill-pending-dispense">
      <i aria-hidden="true" /> Pending dispense
    </span>
  )
}

export function PaymentBadge({
  paymentStatus,
  amountCents,
  invoiceNumber,
}: {
  paymentStatus?: string | null
  amountCents?: number | null
  invoiceNumber?: string | null
}) {
  const status = (paymentStatus || 'unpaid').toLowerCase()
  const amountStr =
    typeof amountCents === 'number' && amountCents > 0
      ? `₹${(amountCents / 100).toFixed(2)}`
      : null

  if (status === 'paid') {
    return (
      <span
        className="rx-pill rx-pill-paid"
        title={invoiceNumber ? `Invoice #${invoiceNumber}` : undefined}
      >
        <i aria-hidden="true" /> Paid {amountStr ? `(${amountStr})` : ''}
        {invoiceNumber && <span className="mono" style={{ marginLeft: 4 }}>{invoiceNumber}</span>}
      </span>
    )
  }

  if (status === 'partial') {
    return (
      <span className="rx-pill rx-pill-partial">
        <i aria-hidden="true" /> Partial {amountStr ? `(${amountStr})` : ''}
      </span>
    )
  }

  return (
    <span className="rx-pill rx-pill-unpaid">
      <i aria-hidden="true" /> Unpaid
    </span>
  )
}

// ---------------------------------------------------------------------
// Payment Modal / Dialog
// ---------------------------------------------------------------------

interface PaymentModalProps {
  prescription: {
    prescription_id: number
    patient_name?: string
    payment_status?: string | null
    payment_amount_cents?: number | null
    payment_method?: string | null
    payment_reference?: string | null
    invoice_number?: string | null
  }
  session: LoginResponse
  onSessionExpired: () => void
  onSuccess: (result: PaymentResponse) => void
  onClose: () => void
}

export function PaymentModal({
  prescription,
  session,
  onSessionExpired,
  onSuccess,
  onClose,
}: PaymentModalProps) {
  const initialRupees =
    prescription.payment_amount_cents != null
      ? (prescription.payment_amount_cents / 100).toString()
      : '350.00'

  const initialStatus: PaymentStatus =
    prescription.payment_status && prescription.payment_status !== 'unpaid'
      ? (prescription.payment_status as PaymentStatus)
      : 'paid'

  const [status, setStatus] = useState<PaymentStatus>(initialStatus)
  const [rupees, setRupees] = useState(initialRupees)
  const [method, setMethod] = useState<PaymentMethod>(
    (prescription.payment_method as PaymentMethod) || 'upi'
  )
  const [reference, setReference] = useState(prescription.payment_reference || '')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !submitting) onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [submitting, onClose])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submitting) return
    setError('')

    const parsedRupees = parseFloat(rupees)
    if (isNaN(parsedRupees) || parsedRupees < 0) {
      setError('Please enter a valid non-negative payment amount.')
      return
    }

    const amountCents = Math.round(parsedRupees * 100)
    if (amountCents > 1000000) {
      setError('Amount exceeds the maximum allowable limit (₹10,000.00).')
      return
    }

    const payload: PaymentRequest = {
      payment_status: status,
      payment_amount_cents: amountCents,
      payment_method: method,
      payment_reference: reference.trim() || undefined,
    }

    setSubmitting(true)
    try {
      const res = await recordPayment(
        prescription.prescription_id,
        payload,
        session.token
      )
      onSuccess(res)
      onClose()
    } catch (err) {
      if (err instanceof ApiCallError && err.status === 401) {
        onSessionExpired()
        return
      }
      setError(
        err instanceof ApiCallError
          ? friendlyMessage(err, 'prescription_payment')
          : 'Failed to record payment. Please try again.'
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
      aria-labelledby="payment-modal-title"
      onClick={(e) => {
        if (e.target === e.currentTarget && !submitting) onClose()
      }}
    >
      <div className="modal-card">
        <div className="modal-header">
          <div>
            <span className="panel-label">BILLING & DISPENSARY</span>
            <h3 id="payment-modal-title">Record Payment</h3>
            <p className="patients-hint">
              Prescription #{prescription.prescription_id}
              {prescription.patient_name ? ` · ${prescription.patient_name}` : ''}
            </p>
          </div>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            disabled={submitting}
            aria-label="Close payment dialog"
          >
            <X size={16} />
          </button>
        </div>

        <form onSubmit={handleSubmit} noValidate>
          <div className="modal-body">
            {error && (
              <div className="inline-error" role="alert">
                {error}
              </div>
            )}

            <label>
              Payment Status
              <select
                value={status}
                onChange={(e) => setStatus(e.target.value as PaymentStatus)}
                disabled={submitting}
              >
                <option value="paid">Paid (Generates invoice number)</option>
                <option value="partial">Partial</option>
                <option value="unpaid">Unpaid</option>
              </select>
            </label>

            <label>
              Amount in INR (₹)
              <input
                type="number"
                step="0.01"
                min="0"
                max="10000"
                value={rupees}
                onChange={(e) => setRupees(e.target.value)}
                placeholder="e.g. 350.00"
                disabled={submitting}
                required
              />
            </label>

            <label>
              Payment Method
              <select
                value={method}
                onChange={(e) => setMethod(e.target.value as PaymentMethod)}
                disabled={submitting}
              >
                <option value="upi">UPI / QR Code</option>
                <option value="cash">Cash</option>
                <option value="card">Debit / Credit Card</option>
              </select>
            </label>

            <label>
              Reference / Note (Optional)
              <input
                type="text"
                maxLength={100}
                value={reference}
                onChange={(e) => setReference(e.target.value)}
                placeholder="e.g. UPI Ref / Cash memo no."
                disabled={submitting}
              />
            </label>
          </div>

          <div className="modal-footer">
            <button
              type="button"
              className="button button-outline"
              onClick={onClose}
              disabled={submitting}
            >
              Cancel
            </button>
            <button type="submit" className="button" disabled={submitting}>
              <CreditCard size={15} />
              {submitting ? 'Saving…' : 'Save Payment'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------
// Invoice Modal / Receipt View & Print
// ---------------------------------------------------------------------

interface InvoiceModalProps {
  prescriptionId: number
  session: LoginResponse
  onSessionExpired: () => void
  onClose: () => void
}

export function InvoiceModal({
  prescriptionId,
  session,
  onSessionExpired,
  onClose,
}: InvoiceModalProps) {
  const [invoice, setInvoice] = useState<InvoiceResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    setError('')

    getInvoice(prescriptionId, session.token, controller.signal)
      .then((res) => {
        if (controller.signal.aborted) return
        setInvoice(res)
        setLoading(false)
      })
      .catch((err) => {
        if (controller.signal.aborted) return
        if (err instanceof ApiCallError && err.status === 401) {
          onSessionExpired()
          return
        }
        setError(
          err instanceof ApiCallError
            ? friendlyMessage(err, 'prescription_invoice')
            : 'Could not load invoice receipt.'
        )
        setLoading(false)
      })

    return () => controller.abort()
  }, [prescriptionId, session.token, attempt, onSessionExpired])

  // Close on Escape key
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', handleKeyDown)
    return () => window.removeEventListener('keydown', handleKeyDown)
  }, [onClose])

  const handlePrint = () => {
    window.print()
  }

  return (
    <div
      className="modal-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="invoice-modal-title"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose()
      }}
    >
      <div className="modal-card" style={{ maxWidth: 620 }}>
        <div className="modal-header">
          <div>
            <span className="panel-label">RECEIPT & INVOICE</span>
            <h3 id="invoice-modal-title">Prescription Invoice</h3>
            <p className="patients-hint">
              Prescription #{prescriptionId}
              {invoice?.invoice_number ? ` · ${invoice.invoice_number}` : ''}
            </p>
          </div>
          <button
            type="button"
            className="modal-close"
            onClick={onClose}
            aria-label="Close invoice dialog"
          >
            <X size={16} />
          </button>
        </div>

        <div className="modal-body">
          {loading && (
            <p className="patients-hint" role="status">
              Loading invoice receipt…
            </p>
          )}

          {error && (
            <div className="patients-error">
              <div className="inline-error" role="alert">
                {error}
              </div>
              <button
                type="button"
                className="button button-outline"
                onClick={() => setAttempt((n) => n + 1)}
              >
                <RefreshCw size={14} /> Try again
              </button>
            </div>
          )}

          {!loading && invoice && (
            <div className="invoice-sheet" id="printable-invoice">
              <div className="invoice-header">
                <div>
                  <div className="brand brand-compact">
                    <span className="brand-mark">🌿</span>
                    AyurCore Clinic
                  </div>
                  <p className="patients-hint" style={{ marginTop: 4 }}>
                    Prescription & Dispensary Receipt
                  </p>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <strong className="mono" style={{ fontSize: 14 }}>
                    {invoice.invoice_number || 'INVOICE PENDING'}
                  </strong>
                  <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 2 }}>
                    Date: {formatRecordDate(invoice.created_at)}
                  </div>
                </div>
              </div>

              <dl className="invoice-meta-grid">
                <div>
                  <dt>Patient Name</dt>
                  <dd>{invoice.patient_display_name}</dd>
                </div>
                <div>
                  <dt>Clinic Patient ID</dt>
                  <dd className="mono">{invoice.patient_clinic_patient_id || 'Not registered'}</dd>
                </div>
                <div>
                  <dt>Dosha Imbalance</dt>
                  <dd>{invoice.dosha_imbalance || 'Not specified'}</dd>
                </div>
                <div>
                  <dt>Dispense Status</dt>
                  <dd>
                    {invoice.dispensed_at ? (
                      <span style={{ color: 'var(--primary)', fontWeight: 600 }}>
                        Dispensed ({formatRecordDate(invoice.dispensed_at)})
                      </span>
                    ) : (
                      <span style={{ color: '#b77932', fontWeight: 600 }}>
                        Pending dispense
                      </span>
                    )}
                  </dd>
                </div>
              </dl>

              <div>
                <div className="panel-label" style={{ marginBottom: 6 }}>
                  PRESCRIBED MEDICINES
                </div>
                <table className="invoice-table" aria-label="Prescription items">
                  <thead>
                    <tr>
                      <th>Herb / Medicine</th>
                      <th style={{ textAlign: 'right' }}>Dispensed Qty</th>
                    </tr>
                  </thead>
                  <tbody>
                    {invoice.prescribed_herbs.map((h, i) => (
                      <tr key={i}>
                        <td>
                          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
                            <Leaf size={13} style={{ color: 'var(--primary)' }} />
                            <strong>{h.name}</strong>
                          </span>
                        </td>
                        <td className="qty">{h.grams} g</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="invoice-summary-box">
                <div>
                  <div style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.08em', fontWeight: 700 }}>
                    Payment Status: {(invoice.payment_status || 'unpaid').toUpperCase()}
                  </div>
                  <div style={{ fontSize: 12, opacity: 0.85, marginTop: 2 }}>
                    Method: {(invoice.payment_method || 'N/A').toUpperCase()}
                    {invoice.payment_reference ? ` · Ref: ${invoice.payment_reference}` : ''}
                  </div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <div style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '.08em' }}>
                    Total Charged
                  </div>
                  <div className="invoice-total-amount">
                    ₹{(invoice.total_amount_cents / 100).toFixed(2)}
                  </div>
                </div>
              </div>

              <div style={{ fontSize: 11, color: 'var(--muted)', textAlign: 'center', marginTop: 4 }}>
                This is a computer-generated clinical dispensary receipt from AyurCore EHR.
              </div>
            </div>
          )}
        </div>

        <div className="modal-footer">
          <button type="button" className="button button-outline" onClick={onClose}>
            Close
          </button>
          {!loading && invoice && (
            <button type="button" className="button" onClick={handlePrint}>
              <Printer size={15} /> Print Invoice
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
