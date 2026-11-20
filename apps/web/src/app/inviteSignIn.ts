import { INVITE_SESSION_KEY, INVITE_TENANT } from '../lib/handoff'

// Kept out of the lazily loaded invite page so the router can ask without loading it.
const SIGN_IN_KEY = 'cloudoptics.do-sign-in' // connect/DigitalOceanConnect.tsx SIGN_IN_KEY

/** Whether DigitalOcean's callback belongs to a connect-invite session (not a signed-in user). */
export function invitedSignIn(): boolean {
  return sessionStorage.getItem(SIGN_IN_KEY) === INVITE_TENANT && sessionStorage.getItem(INVITE_SESSION_KEY) !== null
}
