'use client'

import { useEffect, useId, useMemo, useRef, useState } from 'react'
import {
  ArrowRight,
  Check,
  ChevronDown,
  ChevronLeft,
  ClipboardCheck,
  Clock3,
  Cpu,
  FileText,
  LayoutDashboard,
  Leaf,
  LockKeyhole,
  LogOut,
  Menu,
  Minus,
  Package,
  Plus,
  Search,
  Settings,
  ShieldCheck,
  Sparkles,
  Stethoscope,
  UserRound,
  UsersRound,
  X,
} from 'lucide-react'

import {
  auditEvents as apiAuditEvents,
  auditExport as apiAuditExport,
  getInventory,
  login as apiLogin,
  logout as apiLogout,
  newIdempotencyKey,
  prescribeConfirm,
  prescribeManualPreview,
  prescribePreview,
  prescribePreviewLocal,
  ApiCallError,
} from '@/lib/api'
import { friendlyMessage } from '@/lib/errors'
import { clearSession, loadSession, saveSession, type StoredSession } from '@/lib/session'
import PatientsView from '@/components/patients'
import Dashboard, { Loading, SourceError, useSource } from '@/components/dashboard'
import type {
  AuditEventRecord,
  AuditRecord,
  InventoryItem,
  LoginResponse,
  PrescribedHerb,
  PrescriptionResponse,
  Role,
} from '@/lib/api-types'

// ---------------------------------------------------------------------
// Role-based navigation (backend RBAC remains authoritative -- this only
// decides what the UI *offers*; every underlying call is still enforced
// server-side by backend/rbac.py regardless of what's shown here).
// ---------------------------------------------------------------------

const NAV_BY_ROLE: Record<Role, { id: string; label: string; icon: typeof Stethoscope }[]> = {
  DOCTOR: [
    { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
    { id: 'consultation', label: 'Consultation', icon: Stethoscope },
    { id: 'patients', label: 'Patients', icon: UsersRound },
    { id: 'prescriptions', label: 'Prescriptions', icon: FileText },
    { id: 'inventory', label: 'Inventory', icon: Package },
  ],
  ADMIN: [
    { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
    { id: 'administration', label: 'Administration', icon: Settings },
    { id: 'patients', label: 'Patients', icon: UsersRound },
    { id: 'audit', label: 'Audit', icon: ShieldCheck },
    { id: 'prescriptions', label: 'Prescriptions', icon: FileText },
    { id: 'inventory', label: 'Inventory', icon: Package },
  ],
  AUDITOR: [
    { id: 'dashboard', label: 'Dashboard', icon: LayoutDashboard },
    { id: 'audit', label: 'Audit', icon: ShieldCheck },
    { id: 'prescriptions', label: 'Prescriptions', icon: FileText },
  ],
}

function initials(name: string): string {
  const parts = name.replace(/[._-]+/g, ' ').trim().split(/\s+/)
  if (parts.length === 0 || !parts[0]) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[1][0]).toUpperCase()
}

function formatClinic(clinicId: string): string {
  return clinicId.replace(/_/g, ' ').replace(/\b\w/g, (c) => c.toUpperCase())
}

function formatDate(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      day: '2-digit',
      month: 'short',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    })
  } catch {
    return iso
  }
}

function Brand({ compact = false }: { compact?: boolean }) {
  return (
    <div className={compact ? 'brand brand-compact' : 'brand'}>
      <span className="brand-mark"><Leaf size={15} strokeWidth={1.8} /></span>
      <span>AyurCore</span>
    </div>
  )
}

function Button({ children, variant = 'primary', onClick, type = 'button', className = '', disabled = false }: any) {
  return <button type={type} onClick={onClick} disabled={disabled} className={`button button-${variant} ${className}`}>{children}</button>
}

/** Reused, unstyled-beyond-existing-CSS info banner for a screen this
 * integration could not wire to real data because the AyurCore backend
 * does not currently expose a read endpoint for it. Per this task's
 * explicit instruction, no new backend endpoint was invented to fill
 * these gaps -- see the integration report. */
function NotConnected({ children }: { children: React.ReactNode }) {
  return (
    <div className="inventory-note">
      <ShieldCheck size={17} />
      <span>{children}</span>
    </div>
  )
}

function Landing({ onSignIn }: { onSignIn: () => void }) {
  return (
    <main className="landing-shell">
      <header className="landing-nav wrap">
        <Brand />
        <Button variant="outline" onClick={onSignIn}>Sign in <ArrowRight size={16} /></Button>
      </header>
      <section className="hero wrap">
        <div className="eyebrow"><span className="eyebrow-dot" /> CLINICAL OPERATING SYSTEM</div>
        <h1>Clarity for the<br /><em>practice of care.</em></h1>
        <p className="hero-copy">AI-assisted clinical decision support for Ayurvedic care, designed to keep the doctor at the center of every decision.</p>
        <Button onClick={onSignIn}>Enter AyurCore <ArrowRight size={16} /></Button>
        <div className="workflow" aria-label="Clinical workflow">
          <div className="workflow-step"><span className="workflow-number">01</span><strong>Patient</strong><small>Context & notes</small></div>
          <div className="workflow-line" />
          <div className="workflow-step"><span className="workflow-number">02</span><strong>AI proposes</strong><small>Clinical draft</small></div>
          <div className="workflow-line" />
          <div className="workflow-step"><span className="workflow-number">03</span><strong>Doctor reviews</strong><small>Clinical judgment</small></div>
          <div className="workflow-line" />
          <div className="workflow-step"><span className="workflow-number">04</span><strong>Doctor confirms</strong><small>Final decision</small></div>
        </div>
        <div className="workflow-outcome"><span>↳</span> Audit + Inventory <small>— every confirmation remains traceable</small></div>
      </section>
      <section className="principles wrap">
        <div className="section-kicker">BUILT FOR TRUST</div>
        <div className="principle-grid">
          <div><span>01</span><h3>Clinical review</h3><p>The doctor remains the decision maker. AI proposals are always presented for review.</p></div>
          <div><span>02</span><h3>Inventory integrity</h3><p>Confirmed prescriptions are checked against available clinic stock before they are recorded.</p></div>
          <div><span>03</span><h3>Auditability</h3><p>AI proposals and clinical confirmations remain traceable across the life of a prescription.</p></div>
        </div>
      </section>
      <footer className="landing-footer wrap"><Brand compact /><span>Clinical intelligence, thoughtfully applied.</span><span>© 2026 AyurCore</span></footer>
    </main>
  )
}

function Login({
  onLoginSuccess,
  onBack,
  notice,
}: {
  onLoginSuccess: (session: LoginResponse, remember: boolean) => void
  onBack: () => void
  notice?: string
}) {
  const [showPassword, setShowPassword] = useState(false)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [remember, setRemember] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (submitting) return
    setError('')
    setSubmitting(true)
    try {
      const session = await apiLogin({ username, password })
      onLoginSuccess(session, remember)
    } catch (err) {
      if (err instanceof ApiCallError) {
        setError(friendlyMessage(err, 'login'))
      } else {
        setError('AyurCore can’t be reached right now. Please try again.')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return <main className="login-shell">
    <div className="login-aside"><button className="back-link" onClick={onBack}><ChevronLeft size={16} /> Back to AyurCore</button><div className="login-quote"><span>“</span><p>Technology should make room for better listening.</p><small>AyurCore clinical systems</small></div></div>
    <div className="login-panel"><Brand /><div className="login-form-wrap"><div className="eyebrow"><LockKeyhole size={13} /> SECURE CLINICAL ACCESS</div><h1>Welcome back.</h1><p>Sign in to continue to your clinic workspace.</p>
      {notice && <div className="inline-error" role="status">{notice}</div>}
      <form onSubmit={handleSubmit}>
        <label>Username<input aria-label="Username" autoComplete="username" placeholder="Enter your username" value={username} onChange={(e) => setUsername(e.target.value)} disabled={submitting} /></label>
        <label>Password<div className="password-input"><input aria-label="Password" type={showPassword ? 'text' : 'password'} autoComplete="current-password" placeholder="Enter your password" value={password} onChange={(e) => setPassword(e.target.value)} disabled={submitting} /><button type="button" aria-label={showPassword ? 'Hide password' : 'Show password'} onClick={() => setShowPassword(!showPassword)}>{showPassword ? 'Hide' : 'Show'}</button></div></label>
        <div className="form-meta"><label className="check-label"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} /> <span>Remember this device</span></label><a href="#help">Need help?</a></div>
        {error && <div className="inline-error" role="alert">{error}</div>}
        <Button type="submit" className="full-width" disabled={submitting || !username || !password}>{submitting ? 'Signing in…' : <>Sign in <ArrowRight size={16} /></>}</Button>
      </form>
    </div></div>
  </main>
}

function Sidebar({ active, setActive, onLogout, open, setOpen, session }: any) {
  const navItems = NAV_BY_ROLE[session.role as Role] ?? []
  return <><button className="mobile-menu" aria-label="Open navigation" onClick={() => setOpen(true)}><Menu size={20} /></button><aside className={`sidebar ${open ? 'sidebar-open' : ''}`}><div className="sidebar-top"><Brand /><button className="mobile-close" aria-label="Close navigation" onClick={() => setOpen(false)}><X size={18} /></button></div><div className="role-chip"><span className="avatar">{initials(session.username)}</span><div><strong>{session.username}</strong><small>{session.role}</small><small>{formatClinic(session.clinic_id)}</small></div><ChevronDown size={14} /></div><nav className="side-nav"><span className="nav-label">WORKSPACE</span>{navItems.map(({ id, label, icon: Icon }) => <button key={id} className={active === id ? 'nav-item active' : 'nav-item'} onClick={() => { setActive(id); setOpen(false) }}><Icon size={17} />{label}{active === id && <span className="nav-active-dot" />}</button>)}</nav><div className="sidebar-bottom"><div className="clinic-status"><span className="status-dot" /><div><strong>Clinic systems online</strong><small>Last synced just now</small></div></div><button className="logout-button" onClick={onLogout}><LogOut size={16} /> Sign out</button></div></aside></>
}

function PageHeader({ eyebrow, title, description, action }: any) { return <div className="page-header"><div><div className="section-kicker">{eyebrow}</div><h1>{title}</h1>{description && <p>{description}</p>}</div>{action}</div> }

function PatientContext({ patientName, patientId, manual = false, timestamp }: { patientName: string; patientId?: string; manual?: boolean; timestamp?: string }) {
  return <div className="patient-context"><div className="patient-avatar">{initials(patientName || '?')}</div><div><span>Patient</span><strong>{patientName || 'Unnamed patient'}</strong><small>{patientId || '—'}</small></div><div className="context-divider" /><div><span>Consultation</span><strong>{manual ? 'Manual entry' : timestamp || 'Just now'}</strong><small>General medicine</small></div></div>
}

function MedicineRow({ item, update, remove, editing }: any) { return <div className="medicine-row"><div className="medicine-name"><span className="medicine-icon"><Leaf size={14} /></span><div><strong>{item.name}</strong><small>Canonical formulary</small></div></div><div className="quantity-control"><button aria-label={`Decrease ${item.name}`} onClick={() => update(Math.max(1, item.qty - 1))} disabled={!editing}><Minus size={14} /></button><input aria-label={`${item.name} quantity`} type="number" min="1" value={item.qty} onChange={(e) => update(Math.max(1, Number(e.target.value) || 1))} readOnly={!editing} /><span>g</span><button aria-label={`Increase ${item.name}`} onClick={() => update(item.qty + 1)} disabled={!editing}><Plus size={14} /></button></div><button className="remove-button" onClick={remove} disabled={!editing}>Remove</button></div> }

type ConsultationItem = { name: string; qty: number }

// ---------------------------------------------------------------------
// Slide-to-confirm.
//
// Bug fix (Sep 2026): the previous control was a static layout -- a
// <button onClick={handleConfirm}> next to a "Slide to confirm" label, with
// no pointer/drag handling at all. Dragging the handle never moved it, and
// because the pointer was released away from the button no click reached
// its onClick, so the slide gesture did nothing (mouse and touch alike),
// while a plain click on the handle confirmed instantly.
//
// This keeps the same markup and classes and adds real dragging. It has no
// API knowledge of its own: every path calls the SAME onConfirm handler
// (the existing /prescribe/confirm flow, idempotency key included).
//   - Pointer (mouse, pen, touch): drag the handle past CONFIRM_THRESHOLD of
//     the track to confirm; releasing earlier springs it back. A plain
//     pointer click on the handle does not confirm -- the slide is the
//     deliberate gesture.
//   - Keyboard / assistive tech: the handle is a real <button>. Enter, Space
//     or an assistive-tech "activate" dispatch a click with detail === 0,
//     which confirms.
//   - Single-pointer alternative without dragging (WCAG 2.5.7): the
//     "confirm without sliding" button in the hint line below the track.
// ---------------------------------------------------------------------
const CONFIRM_THRESHOLD = 0.9

function SlideToConfirm({ disabled, busy, onConfirm }: { disabled: boolean; busy: boolean; onConfirm: () => Promise<void> | void }) {
  const hintId = useId()
  const trackRef = useRef<HTMLDivElement>(null)
  const thumbRef = useRef<HTMLButtonElement>(null)
  const dragRef = useRef<{ pointerId: number; startX: number; max: number } | null>(null)
  const firingRef = useRef(false)
  const [offset, setOffset] = useState(0)
  const [dragging, setDragging] = useState(false)
  const inactive = disabled || busy

  // Distance the handle can travel: track inner width minus the handle and
  // the same inset on both ends. Measured on demand, so it follows any
  // responsive width change.
  const travel = () => {
    const track = trackRef.current
    const thumb = thumbRef.current
    if (!track || !thumb) return 0
    return Math.max(0, track.clientWidth - thumb.offsetWidth - thumb.offsetLeft * 2)
  }

  const fire = () => {
    if (inactive || firingRef.current) return
    firingRef.current = true
    setOffset(travel())
    Promise.resolve(onConfirm()).finally(() => {
      // Success unmounts this view; on an error (or a validation early
      // return) the handle springs back so the doctor can try again.
      firingRef.current = false
      setOffset(0)
    })
  }

  const clampedDx = (clientX: number, drag: { startX: number; max: number }) =>
    Math.min(drag.max, Math.max(0, clientX - drag.startX))

  const onPointerDown = (e: React.PointerEvent<HTMLButtonElement>) => {
    if (inactive || firingRef.current) return
    if (e.pointerType === 'mouse' && e.button !== 0) return
    e.currentTarget.setPointerCapture(e.pointerId)
    dragRef.current = { pointerId: e.pointerId, startX: e.clientX, max: travel() }
    setDragging(true)
  }

  const onPointerMove = (e: React.PointerEvent<HTMLButtonElement>) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== e.pointerId) return
    setOffset(clampedDx(e.clientX, drag))
  }

  const endDrag = (e: React.PointerEvent<HTMLButtonElement>, cancelled: boolean) => {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== e.pointerId) return
    dragRef.current = null
    setDragging(false)
    if (!cancelled && drag.max > 0 && clampedDx(e.clientX, drag) >= drag.max * CONFIRM_THRESHOLD) {
      fire()
    } else {
      setOffset(0)
    }
  }

  return <>
    <div ref={trackRef} className={`slider-confirm${dragging ? ' is-dragging' : ''}`}>
      <button
        ref={thumbRef}
        type="button"
        className="slider-thumb"
        aria-label="Confirm prescription"
        aria-describedby={hintId}
        aria-busy={busy || undefined}
        disabled={inactive}
        style={{ transform: `translateX(${offset}px)` }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={(e) => endDrag(e, false)}
        onPointerCancel={(e) => endDrag(e, true)}
        onLostPointerCapture={(e) => endDrag(e, true)}
        onClick={(e) => { if (e.detail === 0) fire() }}
      >
        <ArrowRight size={17} />
      </button>
      <span aria-hidden="true" style={{ opacity: busy ? 1 : Math.max(0.25, 1 - offset / 140) }}>{busy ? 'Confirming…' : 'Slide to confirm'}</span>
    </div>
    <small id={hintId} className="keyboard-hint">
      Drag the handle to the end, or{' '}
      <button type="button" className="confirm-alt" disabled={inactive} onClick={fire}>confirm without sliding</button>
    </small>
    {busy && <span className="visually-hidden" role="status">Confirming prescription…</span>}
  </>
}

function Consultation({ session, onSessionExpired }: { session: LoginResponse; onSessionExpired: () => void }) {
  const [stage, setStage] = useState<'input' | 'review' | 'confirmed'>('input')
  const [mode, setMode] = useState<'ai' | 'manual'>('ai')
  const [proposalSource, setProposalSource] = useState<'local_extractor' | 'gemini' | 'manual'>('local_extractor')
  const [editing, setEditing] = useState(false)
  const [dosha, setDosha] = useState('')
  const [patient, setPatient] = useState('')
  const [patientId, setPatientId] = useState('')
  const [notes, setNotes] = useState('')
  const [items, setItems] = useState<ConsultationItem[]>([])
  const [error, setError] = useState('')
  const [starting, setStarting] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [confirmedResult, setConfirmedResult] = useState<PrescriptionResponse | null>(null)
  const [newHerbName, setNewHerbName] = useState('')
  const [newHerbGrams, setNewHerbGrams] = useState('10')
  const idempotencyKeyRef = useRef<string | null>(null)
  // Re-entrancy guard for handleConfirm: `confirming` state only disables the
  // controls after React re-renders, so this ref is what guarantees a single
  // in-flight /prescribe/confirm call per attempt, whichever control fired it.
  const confirmInFlightRef = useRef(false)

  const handleAuthError = (err: unknown, fallbackOperation?: string): string => {
    if (err instanceof ApiCallError) {
      if (err.status === 401) {
        onSessionExpired()
        return ''
      }
      return friendlyMessage(err, err.operation)
    }
    return 'AyurCore can’t be reached. Check the clinic’s connection, or ask whoever set this up.'
  }

  const resetDraft = () => {
    setStage('input')
    setPatient('')
    setPatientId('')
    setNotes('')
    setItems([])
    setDosha('')
    setProposalSource('local_extractor')
    setPreviewId(null)
    setConfirmedResult(null)
    setError('')
    idempotencyKeyRef.current = null
  }

  const startAiLocal = async () => {
    setError('')
    setStarting(true)
    try {
      const resp = await prescribePreviewLocal(
        { patient_name: patient, patient_id: patientId || undefined, notes },
        session.token
      )
      setPreviewId(resp.preview_id)
      setDosha(resp.dosha_imbalance)
      setItems(resp.prescribed_herbs.map((h) => ({ name: h.name, qty: h.grams })))
      setMode('ai')
      setProposalSource('local_extractor')
      setEditing(false)
      idempotencyKeyRef.current = null
      setStage('review')
    } catch (err) {
      const message = handleAuthError(err)
      if (message) setError(message)
    } finally {
      setStarting(false)
    }
  }

  const startAiGemini = async () => {
    setError('')
    setStarting(true)
    try {
      const resp = await prescribePreview(
        { patient_name: patient, patient_id: patientId || undefined, notes },
        session.token
      )
      setPreviewId(resp.preview_id)
      setDosha(resp.dosha_imbalance)
      setItems(resp.prescribed_herbs.map((h) => ({ name: h.name, qty: h.grams })))
      setMode('ai')
      setProposalSource('gemini')
      setEditing(false)
      idempotencyKeyRef.current = null
      setStage('review')
    } catch (err) {
      const message = handleAuthError(err)
      if (message) setError(message)
    } finally {
      setStarting(false)
    }
  }

  const startManual = () => {
    setError('')
    setMode('manual')
    setProposalSource('manual')
    setDosha('')
    setItems([])
    setPreviewId(null)
    setEditing(true)
    idempotencyKeyRef.current = null
    setStage('review')
  }

  const addMedicine = () => {
    const name = newHerbName.trim()
    const grams = Math.max(1, Number(newHerbGrams) || 0)
    if (!name) {
      setError('Enter a medicine name before adding it.')
      return
    }
    if (items.some((i) => i.name.trim().toLowerCase() === name.toLowerCase())) {
      setError('That medicine is already in the prescription.')
      return
    }
    setItems([...items, { name, qty: grams }])
    setNewHerbName('')
    setNewHerbGrams('10')
    setError('')
  }

  const handleConfirm = async () => {
    if (confirmInFlightRef.current) return
    confirmInFlightRef.current = true
    setError('')
    setConfirming(true)
    try {
      let pid = previewId
      if (mode === 'manual' && !pid) {
        if (!dosha.trim() || items.length === 0) {
          setError('Add a dosha imbalance and at least one medicine before confirming.')
          setConfirming(false)
          confirmInFlightRef.current = false
          return
        }
        const manualResp = await prescribeManualPreview(
          {
            patient_name: patient,
            patient_id: patientId || undefined,
            dosha_imbalance: dosha,
            prescribed_herbs: items.map((i) => ({ name: i.name, grams: i.qty })),
          },
          session.token
        )
        pid = manualResp.preview_id
        setPreviewId(pid)
      }
      if (!idempotencyKeyRef.current) {
        idempotencyKeyRef.current = newIdempotencyKey()
      }
      const result = await prescribeConfirm(
        {
          preview_id: pid as string,
          dosha_imbalance: dosha,
          prescribed_herbs: items.map((i) => ({ name: i.name, grams: i.qty })),
        },
        session.token,
        idempotencyKeyRef.current
      )
      setConfirmedResult(result)
      setStage('confirmed')
    } catch (err) {
      const message = handleAuthError(err)
      if (message) setError(message)
    } finally {
      confirmInFlightRef.current = false
      setConfirming(false)
    }
  }

  if (stage === 'confirmed' && confirmedResult) {
    return <div className="confirmed-state"><div className="success-mark"><Check size={28} /></div><div className="section-kicker">CONFIRMATION COMPLETE</div><h1>Prescription confirmed.</h1><p>The prescription has been recorded and inventory has been validated.</p><div className="confirmation-sheet"><div className="confirm-top"><div><span>Patient</span><strong>{confirmedResult.patient_name}</strong></div><div><span>Date</span><strong>{formatDate(confirmedResult.created_at)}</strong></div><div><span>Dosha</span><strong>{confirmedResult.dosha_imbalance}</strong></div><div><span>Source</span><strong className="mono">{confirmedResult.proposal_source === 'local_extractor' ? 'Local AI Extractor' : confirmedResult.proposal_source === 'gemini' ? 'Google Gemini' : 'Manual Entry'}</strong></div></div><div className="sheet-divider" />{confirmedResult.prescribed_herbs.map((item) => <div className="confirm-row" key={item.name}><span>{item.name}</span><strong>{item.grams} g</strong></div>)}<div className="sheet-divider" /><div className="prescription-number"><span>Prescription reference</span><strong>#{confirmedResult.prescription_id}</strong></div></div><Button onClick={resetDraft}>Start next patient <ArrowRight size={16} /></Button></div>
  }

  return <div className="consultation-view"><PageHeader eyebrow="DOCTOR · CONSULTATION" title={stage === 'input' ? 'New consultation' : mode === 'ai' ? 'Review clinical draft' : 'Write prescription'} description={stage === 'input' ? 'Prepare a clinical proposal for review. The final decision always remains with you.' : mode === 'ai' ? 'Review the proposed prescription before confirming.' : 'Select medicines and quantities for this patient.'} />
    <div className="stage-line"><span className={stage === 'input' ? 'current' : 'done'}>01 <b>INPUT</b></span><i /><span className={stage === 'review' ? 'current' : stage === 'confirmed' ? 'done' : ''}>02 <b>{mode === 'ai' ? 'AI / MANUAL' : 'MANUAL'}</b></span><i /><span>03 <b>REVIEW</b></span><i /><span>04 <b>CONFIRM</b></span></div>
    {stage === 'input' ? <div className="consultation-form"><div className="clinical-form"><label>Patient name<input value={patient} onChange={(e) => setPatient(e.target.value)} placeholder="Search or enter patient name" /></label><label>Patient phone / ID<input value={patientId} onChange={(e) => setPatientId(e.target.value)} placeholder="e.g. AC-1048" /></label><label>Clinical notes<textarea value={notes} onChange={(e) => setNotes(e.target.value)} placeholder="Describe the patient's concerns, observations, and relevant history..." rows={8} /></label><div className="form-footer"><span><ShieldCheck size={15} /> Your notes stay within the clinic workspace.</span></div></div><div className="draft-choice"><div className="choice-kicker">HOW WOULD YOU LIKE TO PROCEED?</div><button className="choice-button" onClick={startAiLocal} disabled={starting || !patient || !notes}><span className="choice-icon"><Cpu size={18} /></span><span><strong>{starting ? 'Drafting…' : 'Draft with Local AI (Offline)'}</strong><small>AI-1 local extractor: T1 tagger + verifier. Offline, deterministic, zero cloud transfer.</small></span><ArrowRight size={17} /></button><button className="choice-button" onClick={startAiGemini} disabled={starting || !patient || !notes}><span className="choice-icon"><Sparkles size={18} /></span><span><strong>{starting ? 'Drafting…' : 'Draft with Cloud AI (Gemini)'}</strong><small>Cloud LLM extraction with client-side PII air-gap safeguard.</small></span><ArrowRight size={17} /></button><button className="choice-button" onClick={startManual} disabled={starting || !patient}><span className="choice-icon manual"><Stethoscope size={18} /></span><span><strong>Write it myself</strong><small>Enter medicines and dosha directly without AI assistance.</small></span><ArrowRight size={17} /></button>{error && <div className="inline-error">{error}</div>}</div></div> : <div className="review-view"><div className="review-header"><div><div className="section-kicker">{mode === 'ai' ? 'AI DRAFT' : 'MANUAL ENTRY'} <span className="generated-status"><span /> {proposalSource === 'local_extractor' ? 'Local Extractor (Offline)' : mode === 'ai' ? 'AI generated' : 'Doctor entered'}</span></div><h2>{mode === 'ai' ? 'Review proposed prescription' : 'Review prescription'}</h2></div><PatientContext patientName={patient} patientId={patientId} manual={mode === 'manual'} /></div><div className="review-grid"><section className="prescription-panel"><div className="panel-heading"><div><span className="panel-label">PRESCRIPTION</span><h3>{editing ? 'Edit medicines and quantities' : 'Proposed medicines'}</h3></div>{!editing && <Button variant="outline" onClick={() => setEditing(true)}>Edit prescription</Button>}</div><div className="dosha-line"><span>Dosha imbalance</span>{editing ? <select value={dosha} onChange={(e) => setDosha(e.target.value)}><option value="">Select…</option><option>Vata</option><option>Pitta</option><option>Kapha</option><option>Other</option></select> : <strong>{dosha}</strong>}</div><div className="medicine-list">{items.map((item, index) => <MedicineRow key={item.name} item={item} editing={editing} update={(qty: number) => setItems(items.map((x, i) => i === index ? { ...x, qty } : x))} remove={() => setItems(items.filter((_, i) => i !== index))} />)}{items.length === 0 && <p style={{ color: 'var(--muted)', fontSize: 12 }}>No medicines added yet.</p>}</div>{editing && <><div className="medicine-row"><div className="medicine-name"><span className="medicine-icon"><Plus size={14} /></span><input aria-label="New medicine name" placeholder="Medicine name" value={newHerbName} onChange={(e) => setNewHerbName(e.target.value)} style={{ border: '1px solid var(--border)', borderRadius: 4, padding: '6px 8px', fontSize: 12 }} /></div><div className="quantity-control"><input aria-label="New medicine grams" type="number" min="1" value={newHerbGrams} onChange={(e) => setNewHerbGrams(e.target.value)} /><span>g</span></div><button className="remove-button" onClick={addMedicine}>Add</button></div>{error && <div className="inline-error">{error}</div>}<div className="edit-actions"><Button variant="outline" onClick={() => setEditing(false)}>Cancel</Button><Button onClick={() => setEditing(false)} disabled={!dosha || items.length === 0}>Save changes <Check size={15} /></Button></div></>}{!editing && error && <div className="inline-error" role="alert">{error}</div>}</section><aside className="confirm-panel"><div className="confirm-panel-top"><span className="panel-label">FINAL REVIEW</span><ShieldCheck size={20} /></div><h3>Ready to confirm?</h3><p>Confirmation will validate stock and record this prescription against the clinic inventory.</p><div className="confirm-rule"><span>Doctor review required</span><Check size={14} /></div><SlideToConfirm disabled={editing || !dosha || items.length === 0} busy={confirming} onConfirm={handleConfirm} /></aside></div></div>}
  </div>
}

/** Current stock of the session's clinic, read-only: GET /inventory (DOCTOR / ADMIN; the backend decides, the clinic is the session's). */
function Inventory({ session, onSessionExpired }: { session: LoginResponse; onSessionExpired: () => void }) {
  const stock = useSource<InventoryItem[]>(true, (signal) => getInventory(session.token, signal).then((r) => r.items), onSessionExpired)
  const items = stock.data
  return <div>
    <PageHeader eyebrow="OPERATIONS · INVENTORY" title="Inventory" description="Current stock across the clinic formulary." />
    {stock.status === 'loading' && <Loading />}
    {stock.status === 'error' && <SourceError error={stock.error} onRetry={stock.reload} />}
    {stock.status === 'done' && items && (items.length === 0
      ? <div className="patients-empty" role="status"><p>No stock is recorded for this clinic.</p></div>
      : <ul className="inventory-grid" aria-label="Stock by herb" style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {items.map((item) => {
          const inStock = item.stock_grams > 0
          return <li className="inventory-row" key={item.herb_name}>
            <div className="medicine-name"><span className="medicine-icon"><Leaf size={14} aria-hidden="true" /></span><div><strong>{item.herb_name}</strong></div></div>
            <div className="stock-amount"><strong>{item.stock_grams.toLocaleString()}</strong> <small>g</small></div>
            <span className={`inventory-status ${inStock ? 'in-stock' : 'out-of-stock'}`}><i aria-hidden="true" />{inStock ? 'In stock' : 'Out of stock'}</span>
          </li>
        })}
      </ul>)}
  </div>
}

function Administration() {
  return <div>
    <PageHeader eyebrow="ADMINISTRATION" title="Administration" description="Clinic and user administration." />
    <NotConnected>
      No administrative endpoints exist on the AyurCore backend yet (user management, clinic configuration,
      and similar). This screen is intentionally left disconnected rather than inventing new administrative
      APIs; see the integration report.
    </NotConnected>
  </div>
}

function herbSummary(herbs: PrescribedHerb[]): string {
  return herbs.map((h) => `${h.name} ${h.grams}g`).join(', ')
}

function Prescriptions({ session, onSessionExpired }: { session: LoginResponse; onSessionExpired: () => void }) {
  const [records, setRecords] = useState<AuditRecord[] | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const canView = session.role === 'ADMIN' || session.role === 'AUDITOR'

  useEffect(() => {
    if (!canView) return
    let cancelled = false
    setLoading(true)
    setError('')
    apiAuditExport(session.token)
      .then((resp) => {
        if (!cancelled) setRecords(resp.records)
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ApiCallError && err.status === 401) {
          onSessionExpired()
          return
        }
        setError(err instanceof ApiCallError ? friendlyMessage(err, err.operation) : 'Something went wrong loading prescriptions.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [canView, session.token])

  if (!canView) {
    return <div>
      <PageHeader eyebrow="WORKSPACE · PRESCRIPTIONS" title="Prescriptions" description="Confirmed clinical records from your clinic." />
      <NotConnected>
        Doctor accounts don't have a prescription-history view yet — the only backend endpoint that lists
        confirmed prescriptions (the audit trail) is restricted to ADMIN/AUDITOR roles by design. This has
        been reported rather than routed around; see the integration report.
      </NotConnected>
    </div>
  }

  return <div>
    <PageHeader eyebrow="WORKSPACE · PRESCRIPTIONS" title="Prescriptions" description="Confirmed prescriptions for your clinic, from the audit trail (patient identity is never stored here by backend design)." />
    {loading && <p style={{ color: 'var(--muted)', fontSize: 12 }}>Loading…</p>}
    {error && <div className="inline-error">{error}</div>}
    {records && <div className="table-wrap"><table><thead><tr><th>Prescription ID</th><th>Date</th><th>Dosha</th><th>Herbs</th><th>Source</th><th>Status</th></tr></thead><tbody>{records.map((r) => <tr key={r.prescription_id}><td><strong className="mono">#{r.prescription_id}</strong></td><td>{formatDate(r.created_at)}</td><td>{r.confirmed_dosha_imbalance}</td><td>{herbSummary(r.confirmed_prescribed_herbs)}</td><td><span className="mono">{r.proposal_source ?? 'gemini'}</span></td><td><span className="status-text"><i />{r.was_changed ? 'Changed from AI' : 'As proposed'}</span></td></tr>)}</tbody></table>{records.length === 0 && <p style={{ padding: 16, color: 'var(--muted)', fontSize: 12 }}>No confirmed prescriptions yet.</p>}</div>}
  </div>
}

function Audit({ session, onSessionExpired }: { session: LoginResponse; onSessionExpired: () => void }) {
  const [records, setRecords] = useState<AuditRecord[] | null>(null)
  const [events, setEvents] = useState<AuditEventRecord[] | null>(null)
  const [summary, setSummary] = useState<{ recordCount: number; changedCount: number; chainValid: boolean; violations: string[]; notes: string[] } | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    setError('')
    Promise.all([apiAuditExport(session.token), apiAuditEvents(session.token)])
      .then(([exportResp, eventsResp]) => {
        if (cancelled) return
        setRecords(exportResp.records)
        setEvents(eventsResp.events)
        setSummary({
          recordCount: exportResp.record_count,
          changedCount: exportResp.changed_count,
          chainValid: eventsResp.chain_valid,
          violations: eventsResp.integrity_violations,
          notes: exportResp.integrity_notes,
        })
      })
      .catch((err) => {
        if (cancelled) return
        if (err instanceof ApiCallError && err.status === 401) {
          onSessionExpired()
          return
        }
        setError(err instanceof ApiCallError ? friendlyMessage(err, err.operation) : 'Something went wrong loading the audit trail.')
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [session.token])

  return <div>
    <PageHeader eyebrow="OVERSIGHT · AUDIT" title="Audit trail" description="AI-proposal-vs-confirmation records and the tamper-evident event log for your clinic." />
    {loading && <p style={{ color: 'var(--muted)', fontSize: 12 }}>Loading…</p>}
    {error && <div className="inline-error">{error}</div>}
    {summary && <div className="inventory-note">
      <ShieldCheck size={17} />
      <span>{summary.recordCount} confirmed record(s), {summary.changedCount} changed from the AI's proposal. Event chain: {summary.chainValid ? 'intact' : 'INTEGRITY VIOLATION DETECTED'}{summary.violations.length > 0 ? ` — ${summary.violations.join('; ')}` : ''}.</span>
    </div>}
    {records && <>
      <div className="panel-heading"><div><span className="panel-label">CORRECTIONS</span><h3>AI proposal vs. confirmation</h3></div></div>
      <div className="table-wrap"><table><thead><tr><th>Prescription ID</th><th>Date</th><th>AI dosha</th><th>Confirmed dosha</th><th>Source</th><th>Changed</th></tr></thead><tbody>{records.map((r) => <tr key={r.prescription_id}><td><strong className="mono">#{r.prescription_id}</strong></td><td>{formatDate(r.created_at)}</td><td>{r.ai_dosha_imbalance}</td><td>{r.confirmed_dosha_imbalance}</td><td><span className="mono">{r.proposal_source ?? 'gemini'}</span></td><td><span className="status-text"><i />{r.was_changed ? 'Yes' : 'No'}</span></td></tr>)}</tbody></table>{records.length === 0 && <p style={{ padding: 16, color: 'var(--muted)', fontSize: 12 }}>No records yet.</p>}</div>
    </>}
    {events && <>
      <div className="panel-heading"><div><span className="panel-label">TAMPER-EVIDENT LOG</span><h3>Audit events</h3></div></div>
      <div className="table-wrap"><table><thead><tr><th>Event ID</th><th>Type</th><th>Prescription</th><th>Actor</th><th>Date</th></tr></thead><tbody>{events.map((ev) => <tr key={ev.event_id}><td className="mono">{ev.event_id}</td><td>{ev.event_type}</td><td><strong className="mono">#{ev.prescription_id}</strong></td><td>{ev.actor_user_id}</td><td>{formatDate(ev.created_at)}</td></tr>)}</tbody></table>{events.length === 0 && <p style={{ padding: 16, color: 'var(--muted)', fontSize: 12 }}>No events yet.</p>}</div>
    </>}
  </div>
}

export default function AyurCoreApp() {
  const [view, setView] = useState<'landing' | 'login' | 'app'>('landing')
  const [session, setSession] = useState<LoginResponse | null>(null)
  const [sessionNotice, setSessionNotice] = useState<string>('')
  const [active, setActive] = useState('consultation')
  const [menuOpen, setMenuOpen] = useState(false)

  // Restore a persisted session on first load (see lib/session.ts) rather
  // than forcing a fresh login on every page refresh.
  useEffect(() => {
    const stored = loadSession()
    if (stored) {
      const restored: LoginResponse = {
        token: stored.token,
        username: stored.username,
        clinic_id: stored.clinic_id,
        role: stored.role,
        expires_at: stored.expires_at,
      }
      setSession(restored)
      setActive(NAV_BY_ROLE[stored.role]?.[0]?.id ?? 'consultation')
      setView('app')
    }
  }, [])

  const handleLoginSuccess = (loginResponse: LoginResponse, remember: boolean) => {
    const stored: StoredSession = {
      token: loginResponse.token,
      username: loginResponse.username,
      clinic_id: loginResponse.clinic_id,
      role: loginResponse.role as Role,
      expires_at: loginResponse.expires_at,
    }
    saveSession(stored, remember)
    setSession(loginResponse)
    setSessionNotice('')
    setActive(NAV_BY_ROLE[loginResponse.role as Role]?.[0]?.id ?? 'consultation')
    setView('app')
  }

  const handleLogout = () => {
    if (session) {
      // Best-effort: revoke server-side immediately, but never block the
      // UI on it -- the local session is cleared regardless.
      apiLogout(session.token).catch(() => {})
    }
    clearSession()
    setSession(null)
    setView('landing')
    setActive('consultation')
  }

  const handleSessionExpired = () => {
    clearSession()
    setSession(null)
    setSessionNotice("Your session has ended. Sign in again to carry on — your notes are kept.")
    setView('login')
  }

  const content = useMemo(() => {
    if (!session) return null
    switch (active) {
      case 'dashboard':
        return <Dashboard session={session} onSessionExpired={handleSessionExpired} onNavigate={setActive} />
      case 'consultation':
        return <Consultation session={session} onSessionExpired={handleSessionExpired} />
      case 'patients':
        // DOCTOR/ADMIN only (NAV_BY_ROLE never offers it to AUDITOR); the
        // backend enforces the same rule on every patient endpoint.
        return <PatientsView session={session} onSessionExpired={handleSessionExpired} />
      case 'prescriptions':
        return <Prescriptions session={session} onSessionExpired={handleSessionExpired} />
      case 'inventory':
        return <Inventory session={session} onSessionExpired={handleSessionExpired} />
      case 'administration':
        return <Administration />
      case 'audit':
        return <Audit session={session} onSessionExpired={handleSessionExpired} />
      default:
        return null
    }
  }, [active, session])

  if (view === 'landing') return <Landing onSignIn={() => setView('login')} />
  if (view === 'login' || !session) return <Login onLoginSuccess={handleLoginSuccess} onBack={() => { setSessionNotice(''); setView('landing') }} notice={sessionNotice} />

  return <div className="app-shell"><Sidebar active={active} setActive={setActive} open={menuOpen} setOpen={setMenuOpen} onLogout={handleLogout} session={session} /><main className="app-main"><div className="app-topbar"><span className="workspace-label">{formatClinic(session.clinic_id).toUpperCase()} <span>/</span> AYURCORE OS</span><div className="topbar-right"><span className="system-online"><i /> Systems online</span><button className="topbar-user"><span className="avatar small">{initials(session.username)}</span><span>{session.username}</span><ChevronDown size={14} /></button></div></div><div className="content-wrap">{content}</div></main></div>
}
