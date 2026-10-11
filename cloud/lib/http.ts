import { SupabaseError } from './supabase.js'

const BASE_HEADERS = {
  'Cache-Control': 'no-store',
  'X-Content-Type-Options': 'nosniff',
}

export function json(body: unknown, status = 200): Response {
  return Response.json(body, { status, headers: BASE_HEADERS })
}

/** Errors are short, Italian and never echo internals to the caller. */
export function error(status: number, message: string): Response {
  return json({ error: message }, status)
}

/** The token from `Authorization: Bearer <token>`, or null. */
export function bearer(request: Request): string | null {
  const header = request.headers.get('authorization') ?? ''
  const match = /^Bearer\s+(\S{8,200})$/i.exec(header.trim())
  return match?.[1] ?? null
}

/** Bin codes as stored in the database: upper case, letters, digits and dashes. */
export function binCode(request: Request): string | null {
  const code = new URL(request.url).searchParams.get('code')?.trim().toUpperCase() ?? ''
  return /^[A-Z0-9][A-Z0-9-]{1,31}$/.test(code) ? code : null
}

/** Reads a JSON body no bigger than `maxBytes`; null when it is missing, too big or not JSON. */
export async function readJson(request: Request, maxBytes: number): Promise<unknown | 'too_large' | null> {
  const declared = Number(request.headers.get('content-length') ?? '0')
  if (declared > maxBytes) return 'too_large'
  const text = await request.text().catch(() => null)
  if (text === null || text.length === 0) return null
  if (text.length > maxBytes) return 'too_large'
  try {
    return JSON.parse(text) as unknown
  } catch {
    return null
  }
}

/** Wraps a handler: a database outage becomes a 502, anything else a 500, both logged on Vercel. */
export function handle(fn: (request: Request) => Promise<Response>) {
  return async (request: Request): Promise<Response> => {
    try {
      return await fn(request)
    } catch (err) {
      console.error(err)
      if (err instanceof SupabaseError) return error(502, 'Database non raggiungibile, riprova tra poco')
      return error(500, 'Errore interno')
    }
  }
}
