import { bearer, error, handle, json, readJson } from '../lib/http.js'
import { batchSchema, MAX_BATCH_BYTES } from '../lib/schemas.js'
import { rpc } from '../lib/supabase.js'

type IngestResult = {
  bin: string
  readings: number
  fill: number
  events: number
  commands: { id: string; command: string; ts: number }[]
}

/**
 * POST /api/ingest — a bin (Arduino UNO Q) sends what it measured since the last sync.
 * Authorization: Bearer <device token>. The answer carries the commands waiting for the bin.
 */
export const POST = handle(async request => {
  const token = bearer(request)
  if (!token) return error(401, 'Token del cassonetto mancante')

  const body = await readJson(request, MAX_BATCH_BYTES)
  if (body === 'too_large') return error(413, 'Invio troppo grande: dividilo in più parti')
  const parsed = batchSchema.safeParse(body)
  if (!parsed.success) return json({ error: 'Dati non validi', issues: parsed.error.issues.slice(0, 5) }, 400)

  const result = await rpc<IngestResult>('ingest_batch', { device_token: token, batch: parsed.data })
  if (!result) return error(401, 'Token del cassonetto non valido')
  return json({ ok: true, ...result })
})
