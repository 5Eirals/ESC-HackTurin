import { z } from 'zod'

/** Unix seconds between 2017 and 2096: catches a board whose clock was never set. */
const unixSeconds = z.number().finite().min(1_500_000_000).max(4_000_000_000)
const measure = z.number().finite().nullable().default(null)

/** One minute of gas, temperature and humidity, averaged on the board. */
export const readingSchema = z.object({
  ts: unixSeconds,
  samples: z.number().int().min(1).max(120),
  temperature_c: measure,
  humidity_pct: measure,
  tvoc_ppb: measure,
  tvoc_max_ppb: z.number().int().nullable().default(null),
  eco2_ppm: measure,
  h2_raw: measure,
  ethanol_raw: measure,
})

/** One row of data/fill_log.csv. */
export const fillSchema = z.object({
  ts: unixSeconds,
  fill_pct: z.number().min(0).max(100),
  peak_pct: z.number().min(0).max(100),
  emptied: z.boolean().default(false),
  source: z.enum(['camera', 'sim']).default('camera'),
})

/** An event for the control centre, with an id that is unique on the board (resending is harmless). */
export const eventSchema = z.object({
  uid: z.string().min(1).max(64),
  ts: unixSeconds,
  type: z.string().regex(/^[A-Z][A-Z_]{1,39}$/),
  severity: z.enum(['info', 'warning', 'critical']),
  title: z.string().trim().min(1).max(200),
  message: z.string().max(1000).default(''),
  values: z.record(z.string(), z.unknown()).default({}),
})

/** What a bin sends at every sync. `status` is the board's own view (alarm, odour, fill prediction…). */
export const batchSchema = z.object({
  address: z.string().trim().max(200).optional(),
  readings: z.array(readingSchema).max(500).default([]),
  fill: z.array(fillSchema).max(3000).default([]),
  events: z.array(eventSchema).max(200).default([]),
  status: z.record(z.string(), z.unknown()).optional(),
})

export type Batch = z.infer<typeof batchSchema>

export const commandSchema = z.object({
  command: z.enum(['mark_emptied']),
})

/** Requests bigger than this are refused before parsing (a month of backfill fits in a few batches). */
export const MAX_BATCH_BYTES = 1_000_000
