/** Supabase settings: the kit's NEXT_PUBLIC_* names work too, so one Vercel environment serves both. */
export function supabaseEnv(): { url: string; key: string } {
  const url = process.env.SUPABASE_URL ?? process.env.NEXT_PUBLIC_SUPABASE_URL
  // Also the names set by the Supabase integration of the Vercel Marketplace (legacy anon key).
  const key =
    process.env.SUPABASE_PUBLISHABLE_KEY ??
    process.env.NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY ??
    process.env.SUPABASE_ANON_KEY ??
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY
  if (!url || !key) {
    throw new Error('Missing SUPABASE_URL or SUPABASE_PUBLISHABLE_KEY: set them in the Vercel project settings.')
  }
  return { url: url.replace(/\/+$/, ''), key }
}

export class SupabaseError extends Error {}

/**
 * Calls a database function through PostgREST with the publishable key. Every function this API uses checks
 * its own token or key, so no session and no service role are involved.
 */
export async function rpc<T>(fn: string, args: Record<string, unknown>): Promise<T | null> {
  const { url, key } = supabaseEnv()
  const headers: Record<string, string> = { apikey: key, 'Content-Type': 'application/json' }
  // Legacy anon keys are JWTs and also go in Authorization; the new sb_publishable_ keys must not.
  if (key.startsWith('eyJ')) headers.Authorization = `Bearer ${key}`

  let response: Response
  try {
    response = await fetch(`${url}/rest/v1/rpc/${fn}`, {
      method: 'POST',
      headers,
      body: JSON.stringify(args),
      signal: AbortSignal.timeout(10_000),
    })
  } catch (err) {
    // Network failure or timeout: the caller answers 502, the board keeps its data and retries.
    throw new SupabaseError(`${fn} unreachable: ${err instanceof Error ? err.message : String(err)}`)
  }
  if (!response.ok) {
    const detail = await response.text().catch(() => '')
    throw new SupabaseError(`${fn} failed with ${response.status}: ${detail.slice(0, 300)}`)
  }
  return (await response.json()) as T | null
}
