// The generated permission data (SITE-08), published for the app's "What Cloudoptics can read"
// panel (docs/14 §6.1) and for anyone reviewing access.
import type { APIRoute } from 'astro'
import { permissions } from '../config'

export const GET: APIRoute = () =>
  new Response(JSON.stringify(permissions, null, 2), { headers: { 'Content-Type': 'application/json' } })
