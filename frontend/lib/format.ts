/** Time zone convention (one, everywhere): the backend stores and returns timestamps as SQLite UTC "YYYY-MM-DD HH:MM:SS" (no zone), and
 *  every date the application shows -- Patient History, the Dashboard, an AI-2 answer and the records it cites -- is that value read as
 *  UTC and displayed in the viewer's own local time zone, by THIS function and no other. An answer's date and its evidence card's date
 *  can therefore never disagree. Anything unrecognised is shown as-is rather than as "Invalid Date". */
export function formatRecordDate(raw: string): string {
  const iso = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$/.test(raw) ? `${raw.replace(' ', 'T')}Z` : raw
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return raw
  return date.toLocaleString(undefined, { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' })
}
