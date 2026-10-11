'use client'

/**
 * Presentation of one validated AI-2 result (lib/ask-view.ts AskView). It receives only what the panel may show -- an answer already worded
 * by lib/ask-present.ts, the cited records, the readings a clarification offers, or a refusal category -- and renders it with the existing
 * visual language (.ask-*, .status-text, .inventory-status, .history-meta / .history-dosha / .history-herbs, .button). It makes no request
 * and chooses nothing: picking a reading only reports its index to the panel, which sends it back through the API client.
 *
 * Reading order of an answer: the question, the verification state, the answer (what it is, the result, a line saying exactly what the
 * result counts or names, labelled details), the qualification immediately beside it when the answer is not exact, then "Based on these
 * records". Cited records are drawn from the reply itself (date, status, dosha, herbs, prescription number), so they read the same whether or
 * not that record is on the loaded page of the patient's history. Labels, encounter references and any internal key are never drawn.
 */

import { forwardRef } from 'react'
import { Leaf } from 'lucide-react'

import { formatRecordDate } from '@/lib/format'
import type { AskView, EvidenceView, NoticeCategory } from '@/lib/ask-view'
import type { Presented } from '@/lib/ask-present'

/** Fixed wording per refusal category. The panel never shows the backend's reason, stage or any text for these. */
const NOTICE: Record<NoticeCategory, { title: string; detail: string }> = {
  missing: { title: 'Nothing is recorded for that.', detail: 'The patient’s record has no entry that answers this question.' },
  unsupported: { title: 'That can’t be answered from the record.', detail: 'Ask about the prescriptions, herbs, doshas or visits recorded for this patient.' },
  unsafe: { title: 'This looks like a question about treatment, advice or diagnosis.', detail: 'This tool only reports what is recorded for the patient. It can’t advise.' },
  access_denied: { title: 'Questions are limited to this patient’s own record.', detail: 'Other patients and clinic-wide questions can’t be asked here.' },
}

/** A question over the reader's limit (ai2/normalize.py: 300 characters, 60 words). Shown instead of the generic "can't be answered" notice. */
const TOO_LONG = { title: 'That question is too long.', detail: 'Questions can be up to 300 characters and 60 words. Shorten it and ask again.' }

type Props = {
  view: AskView
  /** The question this result belongs to (the doctor's own text). */
  asked: string | null
  /** Reports the index of the reading the doctor picked. */
  onChoose: (index: number) => void
  /** Index of the reading being answered right now, or null. */
  busyIndex: number | null
  disabled: boolean
}

function Evidence({ record, focusHerb }: { record: EvidenceView; focusHerb: string | null }) {
  if (record.kind === 'encounter') {
    return <li className="ask-evidence-item">
      <div className="history-meta">
        <strong>Visit</strong>
        <span>{formatRecordDate(record.opened)}</span>
        <span className="status-text"><i aria-hidden="true" />{record.status}</span>
      </div>
      <div className="history-dosha"><span>{record.closed ? 'Closed' : 'Not closed yet'}</span><strong>{record.closed ? formatRecordDate(record.closed) : '—'}</strong></div>
    </li>
  }
  return <li className="ask-evidence-item">
    <div className="history-meta">
      <strong>Prescription</strong>
      <span className="mono">#{record.number}</span>
      <span>{formatRecordDate(record.date)}</span>
      <span className="status-text"><i aria-hidden="true" />{record.status}</span>
    </div>
    <div className="history-dosha"><span>Dosha</span><strong>{record.dosha ?? 'Not recorded'}</strong></div>
    {record.herbs === null
      ? <p className="patients-hint">Herbs not recorded.</p>
      : <ul className="history-herbs" aria-label="Herbs">
        {record.herbs.map((h, i) => <li key={`${h.name}-${i}`} className={focusHerb === h.name ? 'ask-herb-focus' : undefined}><span><Leaf size={13} aria-hidden="true" />{h.name}</span><strong>{h.grams} g</strong></li>)}
      </ul>}
  </li>
}

/** The answer itself: what it is, the result, what the result means, labelled details, a list or a two-record comparison. */
function Answer({ shown }: { shown: Presented }) {
  return <div className="ask-answer-body">
    <h4 className="ask-answer-title">{shown.title}</h4>
    {shown.headline && <p className="ask-answer-headline">{shown.headline}</p>}
    {shown.summary && <p className="ask-answer-summary">{shown.summary}</p>}
    {shown.caveat && <p className="ask-caveat">{shown.caveat}</p>}
    {shown.rows.length > 0 && <dl className="ask-answer-rows">
      {shown.rows.map((row) => <div key={row.label}><dt>{row.label}</dt><dd>{row.value}</dd></div>)}
    </dl>}
    {shown.items.length > 0 && <ol className="ask-answer-items" aria-label={shown.title}>
      {shown.items.map((item, i) => <li key={i}>{item}</li>)}
    </ol>}
    {shown.compare && <table className="ask-compare">
      <thead><tr><td />{shown.compare.columns.map((column) => <th key={column} scope="col">{column}</th>)}</tr></thead>
      <tbody>{shown.compare.rows.map((row) => <tr key={row.label}><th scope="row">{row.label}</th><td>{row.a}</td><td>{row.b}</td></tr>)}</tbody>
    </table>}
  </div>
}

const AskResult = forwardRef<HTMLDivElement, Props>(function AskResult({ view, asked, onChoose, busyIndex, disabled }, ref) {
  if (view.kind === 'answer') {
    return <div ref={ref} tabIndex={-1} className={`ask-result ask-answer${view.incomplete ? ' ask-answer-incomplete' : ''}`}>
      {asked && <small className="ask-asked">You asked: {asked}</small>}
      {view.incomplete
        ? <span className="inventory-status low-stock"><i aria-hidden="true" />Incomplete</span>
        : <span className="status-text"><i aria-hidden="true" />Verified against the record</span>}
      {view.clarifiedAs && <small className="ask-clarified">Answered as: {view.clarifiedAs}</small>}
      <Answer shown={view.shown} />
      <div className="ask-evidence">
        <span className="panel-label">BASED ON THESE RECORDS</span>
        <ul className="ask-evidence-list" aria-label="Records used">
          {view.evidence.map((record, i) => <Evidence key={i} record={record} focusHerb={view.focusHerb} />)}
        </ul>
        {view.evidenceTotal > view.evidence.length && <small>Showing {view.evidence.length} of {view.evidenceTotal} records.</small>}
      </div>
    </div>
  }

  if (view.kind === 'clarify') {
    return <div ref={ref} className="ask-result ask-clarify" role="status">
      {asked && <small className="ask-asked">You asked: {asked}</small>}
      <p>Which of these did you mean?</p>
      <ul className="ask-readings" aria-label="Possible readings">
        {view.options.map((option, i) => <li key={i}>
          <button type="button" className="button button-outline ask-reading" disabled={disabled} aria-busy={busyIndex === i} onClick={() => onChoose(i)}>
            <span>{option.text}</span>
            {busyIndex === i && <small className="ask-reading-busy"> Asking…</small>}
          </button>
        </li>)}
      </ul>
    </div>
  }

  const notice = view.tooLong ? TOO_LONG : NOTICE[view.category]
  return <div ref={ref} tabIndex={-1} className="ask-result ask-notice" role="status" data-category={view.category} data-too-long={view.tooLong ? 'true' : undefined}>
    {asked && <small className="ask-asked">You asked: {asked}</small>}
    <p>{notice.title}</p>
    <small>{notice.detail}</small>
  </div>
})

export default AskResult
