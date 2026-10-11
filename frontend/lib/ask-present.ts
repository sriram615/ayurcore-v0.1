/**
 * Deterministic presentation of one verified AI-2 answer -- a pure function, no React, no network, no model.
 *
 * Why this exists. The backend's `text` is the verifier's template sentence ("sum herb Neem: 40.", "prescription rx:1; created_at ...");
 * it is built for the verifier, not for a doctor, and it is never displayed. The reply already carries everything needed to say the same
 * thing in plain words, so no backend change is needed: the executed query (`plan_query`: op, target, herb, dosha, last_n, a, b), the
 * structured result (`value`), the `qualifier`, and the cited records (`evidence`). This file maps each of the 20 supported operations
 * (ai2/executor.py SUPPORTED) to labelled, human-readable content, and never invents a value, unit, date or interpretation:
 *
 *   - every number, name, date and unit shown comes from `value` or from a cited record;
 *   - dates go through formatRecordDate (lib/format.ts), the same helper as the evidence cards and Patient History;
 *   - prescriptions are named "#<id>" (the number Patient History already shows); the internal label rx:<id>, a visit's reference,
 *     field names and the template sentence never appear.
 *
 * Verification boundary. Presenting must never make a reply look more trustworthy than it is, so before anything is shown the structured
 * result is CROSS-CHECKED against the cited records, using nothing but those records: every record the result refers to must be cited; a
 * date, dosha, herb or gram figure must equal the cited record's; a count or total must equal what the cited records give; a diff must be
 * what the two cited records give; and the qualifier must agree with the records (a result that says "exact" while a cited record has
 * unknown values, or "incomplete" while none has, is contradictory). Anything that does not hold together returns null and the panel shows
 * its fixed "couldn't be shown" message -- the raw output is never a fallback.
 *
 * Counting rules (ai2/executor.py): a count or sum is a lower bound ("at least") when any prescription in scope has unknown herbs;
 * every other incomplete result is "partial"; "unknown" means the result depends entirely on unknown values (value is null).
 */
import { formatRecordDate } from '@/lib/format'
import type { EvidenceView } from '@/lib/ask-view'

export interface PresentedRow { label: string; value: string }

export interface Presented {
  /** What was asked, as a short label: "Total Neem prescribed". */
  title: string
  /** The direct result ("40 g", "4", "10 Oct 2026, 10:22"), or null when nothing can be stated. */
  headline: string | null
  /** One line that says exactly what the headline is (what was counted, which record). */
  summary: string | null
  /** Labelled details (a prescription's dosha and herbs, the changes of a diff). */
  rows: PresentedRow[]
  /** An ordered list (prescriptions, visits, herbs, doshas). */
  items: string[]
  /** A two-record comparison. */
  compare: { columns: [string, string]; rows: { label: string; a: string; b: string }[] } | null
  /** What is incomplete or unknown, in words; present exactly when the answer is not exact. */
  caveat: string | null
}

type Plan = { op: string; target: string; herb: string | null; dosha: string | null; lastN: number | null }
type Rx = Extract<EvidenceView, { kind: 'prescription' }>
type Enc = Extract<EvidenceView, { kind: 'encounter' }>

const isRecord = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
const isInt = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v)
const nonEmpty = (v: unknown): v is string => typeof v === 'string' && v.trim().length > 0
const same = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b)

const SUPPORTED = new Set(['first prescription', 'last prescription', 'first herb', 'last herb', 'first dosha', 'last dosha', 'first encounter', 'last encounter',
  'count prescription', 'count encounter', 'count herb', 'count dosha', 'sum herb', 'list prescription', 'list encounter', 'list herb', 'list dosha',
  'diff prescription', 'compare herb', 'compare dosha'])

const plural = (n: number, one: string, many = `${one}s`) => `${n} ${n === 1 ? one : many}`
const grams = (n: number) => `${n} g`
const numbers = (records: Rx[]) => records.map((r) => `#${r.number}`).join(', ')
const stamp = (r: Rx) => `#${r.number} · ${formatRecordDate(r.date)}`
const herbList = (herbs: [string, number][]) => (herbs.length ? herbs.map(([name, g]) => `${name} ${grams(g)}`).join(', ') : 'None')

function planFrom(raw: unknown): Plan | null {
  if (!isRecord(raw) || !nonEmpty(raw.op) || !nonEmpty(raw.target) || !SUPPORTED.has(`${raw.op} ${raw.target}`)) return null
  if (raw.herb !== null && raw.herb !== undefined && !nonEmpty(raw.herb)) return null
  if (raw.dosha !== null && raw.dosha !== undefined && !nonEmpty(raw.dosha)) return null
  if (raw.last_n !== null && raw.last_n !== undefined && !(isInt(raw.last_n) && raw.last_n > 0)) return null
  return { op: raw.op, target: raw.target, herb: (raw.herb as string | null | undefined) ?? null, dosha: (raw.dosha as string | null | undefined) ?? null, lastN: (raw.last_n as number | null | undefined) ?? null }
}

/** Grams of `herb` in a prescription whose herbs are known (0 when it is not in it), as the backend sums them. */
function gramsOf(rx: Rx, herb: string): number | null {
  return rx.herbs === null ? null : rx.herbs.filter((h) => h.name === herb).reduce((sum, h) => sum + h.grams, 0)
}

const herbPairs = (rx: Rx): [string, number][] | null => (rx.herbs === null ? null : rx.herbs.map((h) => [h.name, h.grams]))

/** The same added / removed / changed sets the executor derives, recomputed from the two cited records, to verify what the reply says. */
function diffOf(a: [string, number][], b: [string, number][]) {
  const sum = (pairs: [string, number][]) => pairs.reduce<Record<string, number>>((acc, [n, g]) => ({ ...acc, [n]: (acc[n] ?? 0) + g }), {})
  const ha = sum(a), hb = sum(b)
  const order = (x: unknown[][]) => [...x].sort((p, q) => (JSON.stringify(p) < JSON.stringify(q) ? -1 : 1))
  return {
    added: order(Object.keys(hb).filter((n) => !(n in ha)).map((n) => [n, hb[n]])),
    removed: order(Object.keys(ha).filter((n) => !(n in hb)).map((n) => [n, ha[n]])),
    changed: order(Object.keys(ha).filter((n) => n in hb && ha[n] !== hb[n]).map((n) => [n, ha[n], hb[n]])),
  }
}

/**
 * An EMPTY list is legitimate in exactly one case: a list of herbs (or doshas) where every cited record is a prescription whose herbs (or dosha)
 * are explicitly unknown, so the backend could list nothing but the unknown prescriptions it cites. Anything else that is empty is refused.
 * Every cited record has already been validated by lib/ask-view.ts (outcome, verified, category, qualifier, evidence schema and label) before
 * this runs; here the records must also be unique, prescription-only, and carry the field the list is about as an EXPLICIT null: an absent
 * property or an undefined value also reads as null in the view, but explicitNull (lib/ask-view.ts) is false for it, so it is refused.
 */
function emptyListJustified(records: EvidenceView[], field: 'herbs' | 'dosha'): boolean {
  if (records.length < 1) return false
  if (new Set(records.map((r) => r.label)).size !== records.length) return false
  return records.every((r) => r.kind === 'prescription' && r[field] === null && r.explicitNull[field] === true)
}

const pairList = (v: unknown): [string, number][] | null =>
  Array.isArray(v) && v.every((p) => Array.isArray(p) && p.length === 2 && nonEmpty(p[0]) && isInt(p[1]) && p[1] > 0) ? (v as [string, number][]) : null

/**
 * `records` are ALL the cited records of the reply, in the order sent. Returns null when the structured result cannot be shown safely.
 * `qualifier` is the reply's own; it is checked against the records, never replaced by them.
 */
export function presentAnswer(rawPlan: unknown, value: unknown, qualifier: string, records: EvidenceView[]): Presented | null {
  const plan = planFrom(rawPlan)
  if (!plan) return null
  const rxs = records.filter((r): r is Rx => r.kind === 'prescription')
  const encs = records.filter((r): r is Enc => r.kind === 'encounter')
  const rxOf = (label: unknown) => (typeof label === 'string' ? rxs.find((r) => r.label === label) : undefined)
  const encOf = (label: unknown) => (typeof label === 'string' ? encs.find((r) => r.label === label) : undefined)

  const { op, target, herb, lastN } = plan
  const scope = lastN ? ` in the latest ${lastN}` : ''
  const herbRecords = herb ? rxs.filter((r) => r.herbs !== null && (gramsOf(r, herb) ?? 0) > 0) : []
  const unknownHerbs = rxs.filter((r) => r.herbs === null)
  const unknownDosha = rxs.filter((r) => r.dosha === null)

  /** The expected qualifier from the cited records, for a lower-bound (count/sum) or partial result. */
  const incompleteQualifier = (isBound: boolean, hasUnknown: boolean) => (!hasUnknown ? 'exact' : isBound ? 'at_least' : 'partial')
  const base = (p: Partial<Presented> & { title: string }): Presented => ({ headline: null, summary: null, rows: [], items: [], compare: null, caveat: null, ...p })
  const expect = (expected: string) => qualifier === expected

  // ---- a result that depends entirely on unknown values: no value, the cited records are the ones that are not recorded
  if (qualifier === 'unknown') {
    if (value !== null || records.length < 1) return null
    const needsHerbs = target === 'herb'
    const missing = needsHerbs ? unknownHerbs : unknownDosha
    if (missing.length !== records.length || !missing.every((r) => r.kind === 'prescription')) return null
    const what = needsHerbs ? 'The herbs are' : 'The dosha is'
    const caveat = `Cannot be determined from the record. ${what} not recorded on ${numbers(missing)}.`
    if (op === 'first' || op === 'last') {
      const title = target === 'herb' ? `${herb} was ${op === 'first' ? 'first' : 'last'} prescribed` : `Dosha on the ${op === 'first' ? 'first' : 'latest'} prescription`
      return base({ title, headline: 'Not known', caveat })
    }
    if (op === 'compare') return base({ title: target === 'herb' ? `${herb} compared between two prescriptions` : 'Dosha compared between two prescriptions', headline: 'Not known', caveat })
    return null
  }

  switch (`${op} ${target}`) {
    case 'first prescription':
    case 'last prescription': {
      if (!isRecord(value)) return null
      const rx = rxOf(value.prescription)
      if (!rx || rx.date !== value.created_at || (rx.dosha ?? null) !== (value.dosha ?? null)) return null
      if (value.herbs === null ? rx.herbs !== null : !same(value.herbs, herbPairs(rx))) return null
      const partial = rx.dosha === null || rx.herbs === null
      if (!expect(partial ? 'partial' : 'exact')) return null
      const first = op === 'first'
      const missing = [rx.dosha === null ? 'dosha' : null, rx.herbs === null ? 'herbs' : null].filter(Boolean).join(' and ')
      return base({
        title: first ? (lastN ? `Earliest of the latest ${lastN} prescriptions` : 'First prescription') : 'Latest prescription',
        headline: `Prescription #${rx.number}`, summary: formatRecordDate(rx.date),
        rows: [{ label: 'Dosha', value: rx.dosha ?? 'Not recorded' }, { label: 'Herbs', value: rx.herbs === null ? 'Not recorded' : herbList(herbPairs(rx) ?? []) }],
        caveat: partial ? `Not recorded on this prescription: ${missing}.` : null,
      })
    }

    case 'first herb':
    case 'last herb': {
      if (!herb || !isRecord(value) || !isInt(value.grams)) return null
      const rx = rxOf(value.prescription)
      if (!rx || rx.date !== value.created_at || gramsOf(rx, herb) !== value.grams) return null
      const others = unknownHerbs
      if (!expect(others.length ? 'partial' : 'exact')) return null
      const which = op === 'first' ? 'first' : 'last'
      return base({
        title: `${herb} was ${which} prescribed${scope}`, headline: formatRecordDate(rx.date), summary: `Prescription #${rx.number} · ${herb} ${grams(value.grams)}`,
        caveat: others.length ? `${plural(others.length, 'prescription')} (${numbers(others)}) ${others.length === 1 ? 'has' : 'have'} herbs not recorded and may also contain ${herb}, so this may not be the ${which} time.` : null,
      })
    }

    case 'first dosha':
    case 'last dosha': {
      if (!isRecord(value) || !nonEmpty(value.dosha)) return null
      const rx = rxOf(value.prescription)
      if (!rx || rx.dosha !== value.dosha || !expect('exact')) return null
      return base({ title: `Dosha on the ${op === 'first' ? (lastN ? `earliest of the latest ${lastN}` : 'first') : 'latest'} prescription`, headline: value.dosha, summary: `Prescription ${stamp(rx)}` })
    }

    case 'first encounter':
    case 'last encounter': {
      if (!isRecord(value) || !expect('exact')) return null
      const enc = encOf(value.encounter)
      if (!enc || enc.opened !== value.opened_at || (enc.closed ?? null) !== (value.closed_at ?? null)) return null
      return base({ title: op === 'first' ? 'First visit' : 'Latest visit', headline: `Opened ${formatRecordDate(enc.opened)}`, summary: enc.closed ? `Closed ${formatRecordDate(enc.closed)}` : 'Not closed yet' })
    }

    case 'count prescription':
      if (!isInt(value) || value < 0 || value !== rxs.length || !expect('exact')) return null
      return base({ title: lastN ? `Prescriptions among the latest ${lastN}` : 'Prescriptions recorded', headline: String(value), summary: 'Number of prescriptions in the record.' })

    case 'count encounter':
      if (!isInt(value) || value < 0 || value !== encs.length || !expect('exact')) return null
      return base({ title: 'Visits recorded', headline: String(value), summary: 'Number of visits in the record.' })

    case 'count herb':
    case 'sum herb': {
      if (!herb || !isInt(value) || value < 0) return null
      const sum = op === 'sum'
      if (value !== (sum ? herbRecords.reduce((s, r) => s + (gramsOf(r, herb) ?? 0), 0) : herbRecords.length)) return null
      if (herbRecords.length + unknownHerbs.length !== rxs.length) return null
      const exactness = incompleteQualifier(true, unknownHerbs.length > 0)
      if (!expect(exactness)) return null
      const unit = sum ? grams(value) : String(value)
      const lead = exactness === 'at_least' ? 'At least ' : ''
      const caveat = exactness === 'at_least'
        ? `${plural(unknownHerbs.length, 'prescription')} (${numbers(unknownHerbs)}) ${unknownHerbs.length === 1 ? 'has' : 'have'} herbs not recorded, so the true ${sum ? 'total' : 'number'} may be higher.` : null
      return base({
        title: sum ? `Total ${herb} prescribed${scope}` : `Prescriptions containing ${herb}${scope}`, headline: `${lead}${unit}`,
        summary: sum ? `The grams recorded for ${herb}, added together over ${plural(herbRecords.length, 'prescription')}.` : `Counts prescriptions that include ${herb}; it is not an amount in grams.`, caveat,
      })
    }

    case 'count dosha': {
      if (!plan.dosha || !isInt(value) || value < 0) return null
      const hits = rxs.filter((r) => r.dosha === plan.dosha)
      if (value !== hits.length || hits.length + unknownDosha.length !== rxs.length) return null
      const exactness = incompleteQualifier(true, unknownDosha.length > 0)
      if (!expect(exactness)) return null
      return base({
        title: `Prescriptions recorded as ${plan.dosha}${scope}`, headline: `${exactness === 'at_least' ? 'At least ' : ''}${value}`, summary: `Counts prescriptions whose recorded dosha is exactly ${plan.dosha}.`,
        caveat: exactness === 'at_least' ? `${plural(unknownDosha.length, 'prescription')} (${numbers(unknownDosha)}) ${unknownDosha.length === 1 ? 'has' : 'have'} no dosha recorded, so the true number may be higher.` : null,
      })
    }

    case 'list prescription': {
      if (!Array.isArray(value) || value.length !== rxs.length || !expect('exact')) return null
      const listed = value.map(rxOf)
      if (listed.some((r) => !r)) return null
      return base({ title: lastN ? `The latest ${lastN} prescriptions, oldest first` : 'Prescriptions, oldest first', headline: String(listed.length), summary: 'Prescriptions recorded.', items: (listed as Rx[]).map(stamp) })
    }

    case 'list encounter': {
      if (!Array.isArray(value) || value.length !== encs.length || !expect('exact')) return null
      const listed = value.map(encOf)
      if (listed.some((e) => !e)) return null
      return base({ title: 'Visits, oldest first', headline: String(listed.length), summary: 'Visits recorded.', items: (listed as Enc[]).map((e) => `Opened ${formatRecordDate(e.opened)}${e.closed ? ` · Closed ${formatRecordDate(e.closed)}` : ' · Not closed yet'}`) })
    }

    case 'list herb': {
      if (!Array.isArray(value) || !value.every(nonEmpty)) return null
      if (new Set(value).size !== value.length) return null
      if (value.length === 0) {
        if (!emptyListJustified(records, 'herbs') || !expect('partial')) return null
        return base({
          title: `Herbs prescribed${scope}`, headline: 'Not known', summary: 'No herbs can be listed: none are recorded on the cited prescriptions.',
          caveat: `${plural(unknownHerbs.length, 'prescription')} (${numbers(unknownHerbs)}) ${unknownHerbs.length === 1 ? 'has' : 'have'} herbs not recorded. This is not a record that no herbs were prescribed.`,
        })
      }
      if (!value.every((name) => rxs.some((r) => r.herbs !== null && (gramsOf(r, name) ?? 0) > 0))) return null
      if (!expect(incompleteQualifier(false, unknownHerbs.length > 0))) return null
      return base({
        title: `Herbs prescribed${scope}`, headline: String(value.length), summary: 'Each herb once, in the order it first appears.', items: value as string[],
        caveat: unknownHerbs.length ? `${plural(unknownHerbs.length, 'prescription')} (${numbers(unknownHerbs)}) ${unknownHerbs.length === 1 ? 'has' : 'have'} herbs not recorded; any herbs in ${unknownHerbs.length === 1 ? 'it' : 'them'} are not listed.` : null,
      })
    }

    case 'list dosha': {
      if (!Array.isArray(value)) return null
      if (value.length === 0) {
        if (!emptyListJustified(records, 'dosha') || !expect('partial')) return null
        return base({
          title: `Dosha by prescription, oldest first${scope}`, headline: 'Not known', summary: 'No dosha can be listed: none is recorded on the cited prescriptions.',
          caveat: `${plural(unknownDosha.length, 'prescription')} (${numbers(unknownDosha)}) ${unknownDosha.length === 1 ? 'has' : 'have'} no dosha recorded. This is not a record that no dosha applied.`,
        })
      }
      const items: string[] = []
      for (const entry of value) {
        const rx = isRecord(entry) ? rxOf(entry.prescription) : undefined
        if (!rx || !isRecord(entry) || !nonEmpty(entry.dosha) || rx.dosha !== entry.dosha) return null
        items.push(`${stamp(rx)} — ${entry.dosha}`)
      }
      if (items.length + unknownDosha.length !== rxs.length) return null
      if (!expect(incompleteQualifier(false, unknownDosha.length > 0))) return null
      return base({
        title: `Dosha by prescription, oldest first${scope}`, headline: String(items.length), summary: 'Prescriptions with a recorded dosha.', items,
        caveat: unknownDosha.length ? `${plural(unknownDosha.length, 'prescription')} (${numbers(unknownDosha)}) ${unknownDosha.length === 1 ? 'has' : 'have'} no dosha recorded and ${unknownDosha.length === 1 ? 'is' : 'are'} not listed.` : null,
      })
    }

    case 'diff prescription': {
      if (!isRecord(value)) return null
      const a = rxOf(value.a), b = rxOf(value.b)
      if (!a || !b || a.label === b.label) return null
      if ((a.dosha ?? null) !== (value.dosha_a ?? null) || (b.dosha ?? null) !== (value.dosha_b ?? null)) return null
      const herbsKnown = a.herbs !== null && b.herbs !== null
      const doshaKnown = a.dosha !== null && b.dosha !== null
      if (herbsKnown) {
        const expected = diffOf(herbPairs(a) ?? [], herbPairs(b) ?? [])
        if (!same(value.added, expected.added) || !same(value.removed, expected.removed) || !same(value.changed, expected.changed)) return null
      } else if (value.added !== null || value.removed !== null || value.changed !== null) return null
      if (value.dosha_changed !== (doshaKnown ? a.dosha !== b.dosha : null)) return null
      const partial = !herbsKnown || !doshaKnown
      if (!expect(partial ? 'partial' : 'exact')) return null

      const added = pairList(value.added) ?? [], removed = pairList(value.removed) ?? []
      const changed = Array.isArray(value.changed) ? (value.changed as [string, number, number][]) : []
      const unknownOf = (pick: (r: Rx) => boolean) => numbers([a, b].filter(pick))
      const rows: PresentedRow[] = herbsKnown
        ? [
          { label: 'Added', value: herbList(added) },
          { label: 'Removed', value: herbList(removed) },
          { label: 'Changed', value: changed.length ? changed.map(([n, from, to]) => `${n} ${grams(from)} → ${grams(to)}`).join(', ') : 'None' },
        ]
        : [{ label: 'Herbs', value: `Cannot be compared: not recorded on ${unknownOf((r) => r.herbs === null)}` }]
      rows.push({ label: 'Dosha', value: doshaKnown ? (a.dosha === b.dosha ? `${a.dosha} (unchanged)` : `${a.dosha} → ${b.dosha}`) : `Cannot be compared: not recorded on ${unknownOf((r) => r.dosha === null)}` })
      return base({
        title: `Changes from #${a.number} to #${b.number}`, summary: `#${a.number} · ${formatRecordDate(a.date)}  →  #${b.number} · ${formatRecordDate(b.date)}`, rows,
        caveat: partial ? 'Some values are not recorded, so this comparison is incomplete.' : null,
      })
    }

    case 'compare herb': {
      if (!herb || !isRecord(value)) return null
      const a = rxOf(value.a), b = rxOf(value.b)
      if (!a || !b || a.label === b.label || !expect('exact')) return null
      const ga = gramsOf(a, herb), gb = gramsOf(b, herb)
      if (ga === null || gb === null || value.grams_a !== ga || value.grams_b !== gb || value.delta !== gb - ga) return null
      const delta = gb - ga
      const amount = (g: number) => (g > 0 ? grams(g) : 'Not prescribed')
      return base({
        title: `${herb} in #${a.number} and #${b.number}`, headline: delta === 0 ? 'No change' : `${delta > 0 ? 'Increased' : 'Decreased'} by ${grams(Math.abs(delta))}`,
        compare: { columns: [stamp(a), stamp(b)], rows: [{ label: herb, a: amount(ga), b: amount(gb) }] },
      })
    }

    case 'compare dosha': {
      if (!isRecord(value) || !expect('exact')) return null
      const a = rxOf(value.a), b = rxOf(value.b)
      if (!a || !b || a.label === b.label || a.dosha === null || b.dosha === null) return null
      if (value.dosha_a !== a.dosha || value.dosha_b !== b.dosha || value.changed !== (a.dosha !== b.dosha)) return null
      return base({
        title: `Dosha in #${a.number} and #${b.number}`, headline: a.dosha === b.dosha ? 'Unchanged' : 'Changed',
        compare: { columns: [stamp(a), stamp(b)], rows: [{ label: 'Dosha', a: a.dosha, b: b.dosha }] },
      })
    }
  }
  return null
}
