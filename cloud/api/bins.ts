import { bearer, error, handle, json } from '../lib/http.js'
import { rpc } from '../lib/supabase.js'

/** GET /api/bins — every bin with its headline state, for the control panel. Bearer <panel key>. */
export const GET = handle(async request => {
  const key = bearer(request)
  if (!key) return error(401, 'Chiave del pannello mancante')
  const bins = await rpc<unknown[]>('panel_bins', { panel_key: key })
  if (!bins) return error(401, 'Chiave del pannello non valida')
  return json({ bins })
})
