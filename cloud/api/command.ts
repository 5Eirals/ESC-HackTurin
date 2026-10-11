import { bearer, binCode, error, handle, json, readJson } from '../lib/http.js'
import { commandSchema } from '../lib/schemas.js'
import { rpc } from '../lib/supabase.js'

/**
 * POST /api/command?code=TO-0001 { "command": "mark_emptied" } — the control panel asks a bin to act.
 * The bin receives it in the answer to its next sync (within a few seconds). Bearer <panel key>.
 */
export const POST = handle(async request => {
  const key = bearer(request)
  if (!key) return error(401, 'Chiave del pannello mancante')
  const code = binCode(request)
  if (!code) return error(400, 'Codice del cassonetto non valido')
  const parsed = commandSchema.safeParse(await readJson(request, 1_000))
  if (!parsed.success) return error(400, 'Comando non valido')

  const result = await rpc<Record<string, unknown>>('panel_command', {
    panel_key: key,
    bin_code: code,
    command: parsed.data.command,
  })
  if (!result) return error(401, 'Chiave del pannello non valida')
  if (result.error === 'not_found') return error(404, 'Cassonetto non trovato')
  if (result.error) return error(400, 'Comando non valido')
  return json({ ok: true, ...result }, 202)
})
