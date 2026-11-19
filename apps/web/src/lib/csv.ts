/** First characters a spreadsheet reads as a formula or a control code (OWASP "CSV injection"). */
const FORMULA_START = ['=', '+', '-', '@', '\t', '\r', '\n']

/** Prefix text a spreadsheet would run as a formula with `'`, so it shows as text (as the API does). */
export function neutralise(value: string): string {
  const risky = (s: string) => FORMULA_START.some((c) => s.startsWith(c))
  return risky(value) || risky(value.replace(/^ +/, '')) ? `'${value}` : value
}

export type CsvValue = string | number | boolean | null | undefined

/** One CSV cell: numbers stay numbers (a credit's -20.00 is not text); other text is neutralised and quoted when needed. */
export function csvCell(value: CsvValue, places = 2): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'number') return value.toFixed(places)
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  const text = neutralise(value)
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text
}

/** A whole CSV file, with a byte order mark so spreadsheets read accents. */
export function toCsv(header: string[], rows: CsvValue[][]): string {
  const line = (cells: CsvValue[]) => cells.map((c) => csvCell(c)).join(',')
  return `\ufeff${[header.map((h) => csvCell(h)).join(','), ...rows.map(line)].join('\r\n')}\r\n`
}

/** Save a downloaded file under its name. */
export function saveFile(blob: Blob, name: string): void {
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.append(a)
  a.click()
  a.remove()
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}
