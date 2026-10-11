import { bearer, binCode, error, handle, json } from '../lib/http.js'
import { rpc } from '../lib/supabase.js'

/**
 * GET /api/bin?code=TO-0001 — one bin in the shape of the board's dashboard snapshot, with the 30-day fill
 * series and the latest events of every bin. Bearer <panel key>.
 */
export const GET = handle(async request => {
  const key = bearer(request)
  if (!key) return error(401, 'Chiave del pannello mancante')
  const code = binCode(request)
  if (!code) return error(400, 'Codice del cassonetto non valido')

  const snapshot = await rpc<Record<string, unknown>>('panel_snapshot', { panel_key: key, bin_code: code })
  if (!snapshot) return error(401, 'Chiave del pannello non valida')
  if (snapshot.error === 'not_found') return error(404, 'Cassonetto non trovato')
  return json(snapshot)
})
