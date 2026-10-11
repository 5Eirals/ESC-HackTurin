import { json } from '../lib/http.js'
import { supabaseEnv } from '../lib/supabase.js'

/** GET /api/health — is the deployment configured? Says nothing about secrets. */
export function GET(): Response {
  let configured = true
  try {
    supabaseEnv()
  } catch {
    configured = false
  }
  return json({ ok: configured, service: 'to-breath-cloud' }, configured ? 200 : 503)
}
