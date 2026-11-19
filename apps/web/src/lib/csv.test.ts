import { describe, expect, it } from 'vitest'

import { csvCell, neutralise, toCsv } from './csv'

describe('CSV cells', () => {
  it('neutralises text a spreadsheet would run as a formula', () => {
    for (const risky of ['=1+1', '+1', '-2', '@SUM(A1)', '\tx', '\rx', '  =HYPERLINK(1)']) {
      expect(neutralise(risky)).toBe(`'${risky}`)
    }
    expect(neutralise('Amazon S3')).toBe('Amazon S3')
  })

  it('keeps numbers as numbers and quotes text with commas or quotes', () => {
    expect(csvCell(-20)).toBe('-20.00')
    expect(csvCell(1.23456, 4)).toBe('1.2346')
    expect(csvCell(null)).toBe('')
    expect(csvCell('a, "b"')).toBe('"a, ""b"""')
    expect(csvCell('=a,b')).toBe(`"'=a,b"`)
    expect(toCsv(['name', 'usd'], [['EC2', 1]])).toBe('\ufeffname,usd\r\nEC2,1.00\r\n')
  })
})
