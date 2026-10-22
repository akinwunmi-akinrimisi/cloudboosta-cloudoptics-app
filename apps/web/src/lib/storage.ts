// Browser storage can be missing or throw (private windows, blocked site data), so every
// access is guarded and the app works without it.

export function read(store: 'local' | 'session', key: string): string | null {
  try {
    return (store === 'local' ? localStorage : sessionStorage).getItem(key)
  } catch {
    return null
  }
}

export function write(store: 'local' | 'session', key: string, value: string | null): void {
  try {
    const s = store === 'local' ? localStorage : sessionStorage
    if (value === null) s.removeItem(key)
    else s.setItem(key, value)
  } catch {
    // ignore: storage unavailable
  }
}
