import { describe, expect, it } from 'vitest'

import { batchSchema, commandSchema } from './schemas.js'

const now = 1_791_650_000

describe('batchSchema', () => {
  it('accepts what the board sends and fills the defaults', () => {
    const parsed = batchSchema.parse({
      address: 'Via Vincenzo Vela 17, Torino',
      readings: [{ ts: now, samples: 60, temperature_c: 21.5, tvoc_ppb: 40 }],
      fill: [{ ts: now, fill_pct: 44.7, peak_pct: 53.3, source: 'sim' }],
      events: [{ uid: 'a1b2-3', ts: now, type: 'BIN_ALMOST_FULL', severity: 'warning', title: 'Quasi pieno' }],
      status: { alarm: { state: 'normal' } },
    })
    expect(parsed.readings[0]?.humidity_pct).toBeNull()
    expect(parsed.fill[0]?.emptied).toBe(false)
    expect(parsed.events[0]?.message).toBe('')
    expect(parsed.events[0]?.values).toEqual({})
  })

  it('accepts an empty batch (a heartbeat)', () => {
    expect(batchSchema.parse({})).toEqual({ readings: [], fill: [], events: [] })
  })

  it('refuses a board whose clock was never set', () => {
    expect(batchSchema.safeParse({ fill: [{ ts: 1000, fill_pct: 10, peak_pct: 10 }] }).success).toBe(false)
  })

  it('refuses out-of-range levels, unknown severities and odd event types', () => {
    expect(batchSchema.safeParse({ fill: [{ ts: now, fill_pct: 120, peak_pct: 10 }] }).success).toBe(false)
    const event = { uid: 'x', ts: now, type: 'GAS_ALERT', severity: 'panic', title: 't' }
    expect(batchSchema.safeParse({ events: [event] }).success).toBe(false)
    expect(batchSchema.safeParse({ events: [{ ...event, severity: 'info', type: 'drop table' }] }).success).toBe(false)
  })

  it('caps the size of each list', () => {
    const readings = Array.from({ length: 501 }, (_, i) => ({ ts: now + i * 60, samples: 60 }))
    expect(batchSchema.safeParse({ readings }).success).toBe(false)
  })
})

describe('commandSchema', () => {
  it('knows only the commands a bin can execute', () => {
    expect(commandSchema.safeParse({ command: 'mark_emptied' }).success).toBe(true)
    expect(commandSchema.safeParse({ command: 'reboot' }).success).toBe(false)
  })
})
